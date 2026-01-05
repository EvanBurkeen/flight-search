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
- "early" flight = departure before 10am (departure_time_before: 10)
- "late" flight = departure after 6pm (departure_time_after: 18)
- "morning" = before 12pm
- "afternoon" = 12pm-6pm
- "evening" = after 6pm

REFUNDABILITY:
- "must be refundable" or "needs to be refundable" → must_be_refundable: true
- "show me refundable" or "prefer refundable" → prefer_refundable: true
- Default: prefer_refundable: false, must_be_refundable: false

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
  try {
    const legs = flightOffer.flights || [];
    if (legs.length === 0) return null;

    const firstLeg = legs[0];
    const lastLeg = legs[legs.length - 1];
    
    // Extract flight info - all optional, don't fail if missing
    const flightNumber = firstLeg.flight_number || '';
    const carrierCode = extractAirlineCode(flightNumber) || null;
    const airline = firstLeg.airline || 'Unknown';
    const alliance = carrierCode ? mapAirlineToAlliance(carrierCode) : 'Independent';
    
    // ONLY reject if explicitly excluded
    if (carrierCode && criteria.exclude_airlines?.includes(carrierCode)) {
      console.log(`Rejected ${airline} (${carrierCode}) - excluded airline`);
      return null;
    }
    
    // ONLY filter alliance if user specified AND we know the alliance
    if (criteria.alliance_preference && 
        criteria.alliance_preference !== 'any' && 
        alliance !== 'Independent' &&
        alliance !== criteria.alliance_preference) {
      console.log(`Rejected ${airline} - wrong alliance (${alliance} vs ${criteria.alliance_preference})`);
      return null;
    }

    // Extract times - don't fail if missing
    const departureTime = firstLeg.departure_airport?.time || firstLeg.departure_time || null;
    const arrivalTime = lastLeg.arrival_airport?.time || lastLeg.arrival_time || null;

    // ONLY filter by time if we have data AND user specified preference
    if (departureTime && (criteria.departure_time_before || criteria.departure_time_after)) {
      try {
        const timeStr = departureTime.includes('T') ? departureTime.split('T')[1] : departureTime;
        const depHour = parseInt(timeStr.split(':')[0]);
        
        if (!isNaN(depHour)) {
          if (criteria.departure_time_before && depHour >= criteria.departure_time_before) {
            console.log(`Rejected ${airline} - too late (${depHour} >= ${criteria.departure_time_before})`);
            return null;
          }
          if (criteria.departure_time_after && depHour < criteria.departure_time_after) {
            console.log(`Rejected ${airline} - too early (${depHour} < ${criteria.departure_time_after})`);
            return null;
          }
        }
      } catch (e) {
        // Continue if time parsing fails
      }
    }

    const price = flightOffer.price || 0;
    const aircraft = firstLeg.airplane || 'Unknown';
    const stops = Math.max(0, legs.length - 1);
    const totalDuration = flightOffer.total_duration || 0;
    
    // ONLY filter must_be_direct if explicitly required
    if (criteria.must_be_direct && stops > 0) {
      console.log(`Rejected ${airline} - not direct (${stops} stops)`);
      return null;
    }

    // Scoring
    let score = 0;
    const highlights: string[] = [];
    
    if (stops === 0) score += 30;
    if (alliance !== 'Independent') score += 5;
    if (criteria.alliance_preference === alliance) score += 15;
    
    const wideBody = ['787', '789', '777', 'A350', 'A380', 'A330'];
    if (wideBody.some(wb => aircraft.includes(wb))) {
      score += 15;
      highlights.push(`✓ ${aircraft}`);
    } else {
      score += 7;
    }
    
    score += 15; // Price component
    
    if (totalDuration && totalDuration < 300) {
      score += 10;
      highlights.push(`✓ Quick (${Math.floor(totalDuration/60)}h)`);
    } else {
      score += 5;
    }

    // Extensions
    const extensions = flightOffer.flights?.flatMap((f: any) => f.extensions || []) || [];
    const extText = extensions.join(' ').toLowerCase();
    
    if (extText.includes('refundable') && !extText.includes('non-refundable')) {
      score += 10;
      highlights.push('✓ Refundable');
    } else if (extText.includes('non-refundable')) {
      // Only reject if user explicitly said "must be refundable"
      if (criteria.must_be_refundable === true) {
        return null;
      }
    }

    // Build URL
    const depCode = criteria.origin || '';
    const arrCode = criteria.destination || '';
    const dateStr = criteria.date || '';
    
    const urls: any = {
      'DL': `https://www.delta.com/flight-search/book-a-flight?origin=${depCode}&destination=${arrCode}&departureDate=${dateStr}`,
      'AA': `https://www.aa.com`,
      'UA': `https://www.united.com`,
      'B6': `https://www.jetblue.com`,
      'NK': `https://www.spirit.com`,
    };
    
    const bookingUrl = (carrierCode && urls[carrierCode]) || `https://www.google.com/travel/flights?q=${depCode}%20to%20${arrCode}`;

    const details: any = {};
    if (extText.includes('refundable')) details.refundable = 'Refundable';
    if (extText.includes('non-refundable')) details.refundable = 'Non-refundable';
    if (extText.includes('carry-on')) details.carry_on = 'Included';
    if (extText.includes('checked bag')) details.checked_bags = 'Included';

    return {
      airline,
      airline_code: carrierCode || 'N/A',
      alliance,
      price,
      aircraft,
      departure_time: departureTime,
      arrival_time: arrivalTime,
      duration: totalDuration,
      stops,
      cabin_class: criteria.primary_cabin,
      score,
      highlights,
      warnings: [],
      booking_url: bookingUrl,
      details,
      raw_extensions: extensions.slice(0, 2),
    };
  } catch (error) {
    console.error('Flight eval error:', error);
    return null;
  }
}

export async function POST(request: NextRequest) {
  try {
    const { query, searchType, selectedOutbound } = await request.json();

    if (!query) {
      return NextResponse.json({ error: 'Query is required' }, { status: 400 });
    }

    // Parse query with LLM
    const criteria = await parseQueryWithLLM(query);
    
    console.log('Parsed criteria:', JSON.stringify(criteria, null, 2));

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

    console.log(`Found ${flights.length} flights from SerpAPI, evaluating...`);

    // Evaluate and rank
    const results = flights
      .map((flight: any) => evaluateFlight(flight, criteria))
      .filter((r: any) => r !== null)
      .sort((a: any, b: any) => b.score - a.score);

    console.log(`After filtering: ${results.length} flights remain`);

    if (results.length === 0) {
      return NextResponse.json({
        message: '❌ No flights found matching your criteria. Try:\n• Different dates\n• Relaxing requirements\n• Different airports',
        results: [],
      });
    }

    // Add "Direct flight" highlight only if there's a mix
    const hasDirectFlights = results.some((r: any) => r.stops === 0);
    const hasConnectingFlights = results.some((r: any) => r.stops > 0);
    
    if (hasDirectFlights && hasConnectingFlights) {
      results.forEach((r: any) => {
        if (r.stops === 0) {
          r.highlights.unshift('✓ Direct');
        }
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
