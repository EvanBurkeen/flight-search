import { NextRequest, NextResponse } from 'next/server';
import Anthropic from '@anthropic-ai/sdk';
import axios from 'axios';

const anthropic = new Anthropic({
  apiKey: process.env.ANTHROPIC_API_KEY!,
});

// Alliance mappings
const SKYTEAM = ['DL', 'VS', 'AF', 'KL', 'AZ', 'AM', 'AR', 'SU', 'CZ', 'MU', 'VN', 'ME', 'KQ', 'RO', 'OK'];
const ONEWORLD = ['AA', 'BA', 'IB', 'QR', 'QF', 'JL', 'CX', 'FJ', 'AY', 'AS', 'RJ'];
const STAR_ALLIANCE = ['UA', 'LH', 'AC', 'SQ', 'NH', 'OS', 'SK', 'LX', 'TP', 'TK', 'SA'];

function mapAirlineToAlliance(carrierCode: string): string {
  if (SKYTEAM.includes(carrierCode)) return 'SkyTeam';
  if (ONEWORLD.includes(carrierCode)) return 'OneWorld';
  if (STAR_ALLIANCE.includes(carrierCode)) return 'Star Alliance';
  return 'Independent';
}

function extractAirlineCode(flightNumber: string): string | null {
  if (!flightNumber) return null;
  const code = flightNumber.includes(' ') ? flightNumber.split(' ')[0] : flightNumber.slice(0, 2);
  return code.toUpperCase();
}

async function parseQueryWithLLM(query: string) {
  const systemPrompt = `You are a flight search query parser. Extract structured information from natural language flight requests.

IMPORTANT: Today's date is January 5, 2026. When parsing dates:
- If a date like "2/5" is mentioned, assume it means 2026-02-05 (February 5, 2026)
- Always output dates in YYYY-MM-DD format

MULTIPLE AIRPORTS:
- If user mentions "JFK/EWR" or "JFK or EWR", use the FIRST one as origin (JFK)
- Note the alternative in special_instructions

TIME PREFERENCES:
- "early" flight = departure before 10am
- "late" flight = departure after 6pm
- "morning" = before 12pm
- "afternoon" = 12pm-6pm
- "evening" = after 6pm

Return a JSON object with these fields:
{
  "origin": "3-letter airport code (first if multiple mentioned)",
  "destination": "3-letter airport code",
  "date": "YYYY-MM-DD",
  "return_date": "YYYY-MM-DD or null",
  "is_roundtrip": true/false,
  "primary_cabin": "economy/premium_economy/business/first",
  "compare_cabins": ["list of cabins to compare"],
  "alliance_preference": "SkyTeam/OneWorld/Star Alliance/any (default: any)",
  "loyalty_program": "delta/united/american/etc or null (only if user mentions it)",
  "specific_airlines": ["airline codes"],
  "exclude_airlines": ["airline codes to exclude"],
  "departure_time_after": "hour or null (e.g., 18 for after 6pm)",
  "departure_time_before": "hour or null (e.g., 10 for before 10am)",
  "return_time_after": "hour or null (for return flight)",
  "return_time_before": "hour or null (for return flight)",
  "must_be_refundable": true/false,
  "prefer_refundable": true/false,
  "must_be_direct": true/false,
  "prefer_direct": true/false,
  "needs_extra_legroom": true/false,
  "price_sensitivity": "very_sensitive/sensitive/moderate/flexible/luxury",
  "special_instructions": "any other notes like alternative airports",
  "confidence": "high/medium/low"
}

Return ONLY valid JSON, no other text.`;

  const message = await anthropic.messages.create({
    model: 'claude-sonnet-4-20250514',
    max_tokens: 1000,
    system: systemPrompt,
    messages: [{ role: 'user', content: query }],
  });

  let responseText = message.content[0].type === 'text' ? message.content[0].text : '';
  
  // Clean up response
  if (responseText.startsWith('```')) {
    responseText = responseText.split('```')[1];
    if (responseText.startsWith('json')) {
      responseText = responseText.slice(4);
    }
    responseText = responseText.trim();
  }

  return JSON.parse(responseText);
}

async function searchGoogleFlights(
  origin: string,
  destination: string,
  date: string,
  travelClass: number = 1,
  returnDate?: string,
  excludeAirlines?: string[]
) {
  const params: any = {
    engine: 'google_flights',
    departure_id: origin,
    arrival_id: destination,
    outbound_date: date,
    currency: 'USD',
    hl: 'en',
    adults: 1,
    type: returnDate ? '1' : '2',
    travel_class: String(travelClass),
    api_key: process.env.SERP_API_KEY!,
  };

  if (returnDate) {
    params.return_date = returnDate;
  }

  if (excludeAirlines && excludeAirlines.length > 0) {
    params.exclude_airlines = excludeAirlines.join(',');
  }

  const response = await axios.get('https://serpapi.com/search.json', { params });
  
  const flights = [
    ...(response.data.best_flights || []),
    ...(response.data.other_flights || []),
  ];

  return flights;
}

function evaluateFlight(flightOffer: any, criteria: any) {
  const legs = flightOffer.flights || [];
  if (legs.length === 0) return null;

  const firstLeg = legs[0];
  const lastLeg = legs[legs.length - 1];

  const flightNumber = firstLeg.flight_number || '';
  const carrierCode = extractAirlineCode(flightNumber);
  if (!carrierCode) return null;

  const alliance = mapAirlineToAlliance(carrierCode);

  // Check alliance preference
  if (criteria.alliance_preference && criteria.alliance_preference !== 'any') {
    if (alliance !== criteria.alliance_preference) return null;
  }

  // Check excluded airlines
  if (criteria.exclude_airlines && criteria.exclude_airlines.includes(carrierCode)) {
    return null;
  }

  // Check time preferences
  if (firstLeg.departure_airport?.time) {
    const depTime = firstLeg.departure_airport.time;
    const depHour = parseInt(depTime.split('T')[1]?.split(':')[0] || '0');
    
    if (criteria.departure_time_before && depHour >= criteria.departure_time_before) {
      return null; // Too late
    }
    if (criteria.departure_time_after && depHour < criteria.departure_time_after) {
      return null; // Too early
    }
  }

  const airline = firstLeg.airline || 'Unknown';
  const price = flightOffer.price || 0;
  const aircraft = firstLeg.airplane || 'Unknown';
  const stops = legs.length - 1;

  // Check direct flight requirement
  if (criteria.must_be_direct && stops > 0) return null;

  const totalDuration = flightOffer.total_duration || 0;

  // Scoring (general quality metrics)
  let score = 0;
  const highlights: string[] = [];
  const warnings: string[] = [];

  // 1. Direct flight bonus (30 points) - Most valuable to travelers
  if (stops === 0) {
    score += 30;
    highlights.push('✓ Direct flight');
  } else if (criteria.prefer_direct) {
    warnings.push(`⚠ ${stops} stop(s)`);
  }

  // 2. Alliance/Loyalty (20 points) - Only if user specified
  if (criteria.alliance_preference && criteria.alliance_preference !== 'any') {
    if (alliance === criteria.alliance_preference) {
      score += 20;
      highlights.push(`✓ ${alliance} alliance`);
      if (criteria.loyalty_program) {
        highlights.push(`✓ Your loyalty benefits apply`);
      }
    } else {
      warnings.push(`⚠ ${alliance} - Not your preferred alliance`);
    }
  } else {
    // Give small bonus to major alliances for general quality
    if (alliance !== 'Independent') {
      score += 5;
    }
  }

  // 3. Aircraft comfort (15 points)
  const wideBodyAircraft = ['787', '789', '788', '77W', '777', '359', '35K', '351', 'A350', 'A380', 'A330'];
  if (wideBodyAircraft.some(wb => aircraft.includes(wb))) {
    score += 15;
    highlights.push(`✓ Excellent comfort (${aircraft})`);
  } else if (criteria.needs_extra_legroom) {
    score += 7;
    warnings.push(`⚠ Standard aircraft (${aircraft})`);
  } else {
    score += 7;
  }

  // 4. Duration (10 points) - Shorter is better
  const avgDuration = 420; // 7 hours average
  if (totalDuration < avgDuration) {
    score += 10;
    if (totalDuration < 300) {
      highlights.push('✓ Quick flight');
    }
  } else if (totalDuration > avgDuration * 1.5) {
    warnings.push('⚠ Long travel time');
  }

  // 5. Price scoring (15 points) - Will be adjusted relative to other flights
  score += 15;

  // 6. Refundability (10 points)
  // Check extensions for refundability info
  const extensions = flightOffer.flights?.flatMap((f: any) => f.extensions || []) || [];
  const extensionsText = extensions.join(' ').toLowerCase();
  
  if (extensionsText.includes('refundable') && !extensionsText.includes('non-refundable')) {
    score += 10;
    highlights.push('✓ Refundable');
  } else if (extensionsText.includes('non-refundable')) {
    if (criteria.must_be_refundable) {
      return null; // Filter out non-refundable if required
    }
    if (criteria.prefer_refundable) {
      warnings.push('⚠ Non-refundable');
    }
  }

  const finalScore = score; // Out of 100

  // Build direct airline booking URL
  let bookingUrl = null;
  const depCode = criteria.origin;
  const arrCode = criteria.destination;
  const dateStr = criteria.date;
  const returnDateStr = criteria.return_date || '';
  
  // Airline-specific booking URLs
  const airlineBookingUrls: { [key: string]: string } = {
    // Major US carriers
    'DL': `https://www.delta.com/flight-search/book-a-flight?origin=${depCode}&destination=${arrCode}&departureDate=${dateStr}&returnDate=${returnDateStr}`,
    'AA': `https://www.aa.com/booking/search?locale=en_US&origin=${depCode}&destination=${arrCode}&departDate=${dateStr}&returnDate=${returnDateStr}`,
    'UA': `https://www.united.com/en/us/fsr/choose-flights?f=${depCode}&t=${arrCode}&d=${dateStr}&r=${returnDateStr}`,
    'WN': `https://www.southwest.com/air/booking/select.html?originationAirportCode=${depCode}&destinationAirportCode=${arrCode}&departureDate=${dateStr}&returnDate=${returnDateStr}`,
    'B6': `https://www.jetblue.com/booking/flights?from=${depCode}&to=${arrCode}&depart=${dateStr}&return=${returnDateStr}`,
    'AS': `https://www.alaskaair.com/shopping/flights?fromLocation=${depCode}&toLocation=${arrCode}&departureDate=${dateStr}&returnDate=${returnDateStr}`,
    
    // Budget carriers
    'NK': `https://book.spirit.com/Flight/Select?culture=en-US&dep=${depCode}&arr=${arrCode}&date=${dateStr}&ret=${returnDateStr}`,
    'F9': `https://www.flyfrontier.com/travel/flight-search/?departureDate=${dateStr}&destinationCode=${arrCode}&numAdults=1&originCode=${depCode}&returnDate=${returnDateStr}`,
    'G4': `https://www.allegiantair.com/booking/flights?dep=${depCode}&arr=${arrCode}&date=${dateStr}`,
    
    // International carriers (major ones)
    'BA': `https://www.britishairways.com/travel/book/public/en_us?eId=106019&bookingFor=ECONOMY&from=${depCode}&to=${arrCode}&departureDate=${dateStr}&returnDate=${returnDateStr}`,
    'AF': `https://wwws.airfrance.us/search/offers?connections=1&activeConnection=0&cabinClass=ECONOMY&adults=1&origin=${depCode}&destination=${arrCode}&departureDate=${dateStr}&returnDate=${returnDateStr}`,
    'KL': `https://www.klm.com/search/offers?origin=${depCode}&destination=${arrCode}&departureDate=${dateStr}&returnDate=${returnDateStr}`,
    'LH': `https://www.lufthansa.com/us/en/flight-search?origin=${depCode}&destination=${arrCode}&outbound-date=${dateStr}&return-date=${returnDateStr}`,
    'VS': `https://flywith.virginatlantic.com/en-us/book/flights?origin=${depCode}&destination=${arrCode}&departureDate=${dateStr}&returnDate=${returnDateStr}`,
    'AC': `https://www.aircanada.com/us/en/aco/home/book/search-book.html?org0=${depCode}&dest0=${arrCode}&date0=${dateStr}&date1=${returnDateStr}`,
    'QR': `https://www.qatarairways.com/en-us/booking.html?origin=${depCode}&destination=${arrCode}&departingDate=${dateStr}&returningDate=${returnDateStr}`,
    'EK': `https://www.emirates.com/us/english/search/?orig=${depCode}&dest=${arrCode}&date1=${dateStr}&date2=${returnDateStr}`,
  };
  
  // Try to get airline-specific URL
  if (carrierCode && airlineBookingUrls[carrierCode]) {
    bookingUrl = airlineBookingUrls[carrierCode];
  }
  
  // Fallback: generic airline website
  if (!bookingUrl) {
    const airlineWebsites: { [key: string]: string } = {
      'DL': 'https://www.delta.com',
      'AA': 'https://www.aa.com',
      'UA': 'https://www.united.com',
      'WN': 'https://www.southwest.com',
      'B6': 'https://www.jetblue.com',
      'AS': 'https://www.alaskaair.com',
      'NK': 'https://www.spirit.com',
      'F9': 'https://www.flyfrontier.com',
      'BA': 'https://www.britishairways.com',
      'AF': 'https://www.airfrance.us',
      'KL': 'https://www.klm.com',
      'LH': 'https://www.lufthansa.com',
      'VS': 'https://www.virginatlantic.com',
      'AC': 'https://www.aircanada.com',
      'QR': 'https://www.qatarairways.com',
      'EK': 'https://www.emirates.com',
    };
    
    bookingUrl = airlineWebsites[carrierCode] || `https://www.google.com/search?q=${airline}+flights+${depCode}+to+${arrCode}`;
  }

  // Detailed flight information (using extensions already defined above)
  const flightDetails: any = {
    refundable: null,
    change_fee: null,
    checked_bags: null,
    carry_on: null,
    seat_selection: null,
    fare_class: null,
  };

  // Parse refundability
  if (extensionsText.includes('free cancellation')) {
    flightDetails.refundable = 'Free cancellation';
  } else if (extensionsText.includes('refundable')) {
    flightDetails.refundable = 'Refundable';
  } else if (extensionsText.includes('non-refundable')) {
    flightDetails.refundable = 'Non-refundable';
  }

  // Parse baggage
  if (extensionsText.includes('checked bag') || extensionsText.includes('1 checked bag')) {
    flightDetails.checked_bags = 'Included';
  } else if (extensionsText.includes('no checked bag')) {
    flightDetails.checked_bags = 'Not included';
  }

  if (extensionsText.includes('carry-on') || extensionsText.includes('personal item')) {
    flightDetails.carry_on = 'Included';
  } else if (extensionsText.includes('no carry-on')) {
    flightDetails.carry_on = 'Not included';
  }

  // Parse change fees
  if (extensionsText.includes('free changes') || extensionsText.includes('no change fee')) {
    flightDetails.change_fee = 'Free changes';
  } else if (extensionsText.includes('change fee')) {
    flightDetails.change_fee = 'Change fee applies';
  }

  // Parse seat selection
  if (extensionsText.includes('free seat selection')) {
    flightDetails.seat_selection = 'Free';
  } else if (extensionsText.includes('seat selection')) {
    flightDetails.seat_selection = 'Available for fee';
  }

  // Parse fare class
  const fareMatch = extensionsText.match(/(basic economy|economy|premium economy|business|first class)/i);
  if (fareMatch) {
    flightDetails.fare_class = fareMatch[1];
  }

  return {
    airline,
    airline_code: carrierCode,
    alliance,
    price,
    aircraft,
    departure_time: firstLeg.departure_airport?.time,
    arrival_time: lastLeg.arrival_airport?.time,
    duration: totalDuration,
    stops,
    cabin_class: criteria.primary_cabin,
    score: finalScore,
    highlights,
    warnings,
    booking_url: bookingUrl,
    details: flightDetails,
    raw_extensions: extensions.slice(0, 5), // Include raw extensions for display
  };
}

export async function POST(request: NextRequest) {
  try {
    const { query, searchType, selectedOutbound } = await request.json();

    if (!query) {
      return NextResponse.json({ error: 'Query is required' }, { status: 400 });
    }

    // Parse query with LLM
    const criteria = await parseQueryWithLLM(query);

    if (!criteria.origin || !criteria.destination || !criteria.date) {
      return NextResponse.json(
        {
          message: '⚠️ I need to know:\n• Origin airport (e.g., JFK)\n• Destination airport (e.g., LHR)\n• Travel date (e.g., 3/21)',
          results: [],
        },
        { status: 200 }
      );
    }

    // Determine if this is a round trip
    const isRoundTrip = !!criteria.return_date;
    
    // Determine search parameters based on search type
    let searchOrigin, searchDestination, searchDate;
    
    if (searchType === 'return' && isRoundTrip) {
      // Return flight: reverse direction, use return date
      searchOrigin = criteria.destination;
      searchDestination = criteria.origin;
      searchDate = criteria.return_date;
    } else {
      // Outbound or one-way flight
      searchOrigin = criteria.origin;
      searchDestination = criteria.destination;
      searchDate = criteria.date;
    }

    // Map cabin to code
    const cabinToCode: { [key: string]: number } = {
      economy: 1,
      premium_economy: 2,
      business: 3,
      first: 4,
    };

    const travelClass = cabinToCode[criteria.primary_cabin] || 1;

    // Search flights (always one-way for multi-step)
    const flights = await searchGoogleFlights(
      searchOrigin,
      searchDestination,
      searchDate,
      travelClass,
      undefined, // No return date - searching one-way
      criteria.exclude_airlines
    );

    if (flights.length === 0) {
      return NextResponse.json({
        message: '❌ No flights found for your search. Try different dates or airports.',
        results: [],
      });
    }

    // Evaluate and rank
    const results = flights
      .map((flight: any) => evaluateFlight(flight, criteria))
      .filter((r: any) => r !== null)
      .sort((a: any, b: any) => b.score - a.score);

    if (results.length === 0) {
      return NextResponse.json({
        message: '❌ No flights match your criteria. Try relaxing some requirements.',
        results: [],
      });
    }

    // Build response message
    let message = '';
    
    if (searchType === 'return') {
      message = `Return flights ${searchOrigin} → ${searchDestination}\n`;
      message += `Date: ${searchDate}\n`;
      message += `\nSelect your return flight (prices below are one-way):`;
    } else if (isRoundTrip) {
      message = `Outbound flights ${searchOrigin} → ${searchDestination}\n`;
      message += `Date: ${searchDate}\n`;
      if (criteria.special_instructions) {
        message += `Note: ${criteria.special_instructions}\n`;
      }
      message += `\nSelect your outbound flight. After you select, I'll search return flights (takes a few seconds):`;
    } else {
      message = `Found ${results.length} flights\n\n`;
      message += `Route: ${searchOrigin} → ${searchDestination}\n`;
      message += `Date: ${searchDate}\n`;
      message += `Cabin: ${criteria.primary_cabin.replace('_', ' ')}\n`;
      if (criteria.alliance_preference && criteria.alliance_preference !== 'any') {
        message += `Alliance: ${criteria.alliance_preference}\n`;
      }
      if (criteria.exclude_airlines && criteria.exclude_airlines.length > 0) {
        message += `Excluded: ${criteria.exclude_airlines.join(', ')}\n`;
      }
      if (criteria.special_instructions) {
        message += `Note: ${criteria.special_instructions}\n`;
      }
      message += `\nTop ${Math.min(5, results.length)} options ranked by quality and value`;
    }

    return NextResponse.json({
      message,
      results: results.slice(0, 10),
      isRoundTrip: isRoundTrip && searchType !== 'return',
    });
  } catch (error: any) {
    console.error('Search error:', error);
    return NextResponse.json(
      { error: error.message || 'Internal server error' },
      { status: 500 }
    );
  }
}
