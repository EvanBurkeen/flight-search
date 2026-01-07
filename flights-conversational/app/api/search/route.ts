import { NextResponse } from 'next/server';
import Anthropic from '@anthropic-ai/sdk';

const anthropic = new Anthropic({
  apiKey: process.env.ANTHROPIC_API_KEY || '',
});

// Multi-airport city mappings for intelligent search
const AIRPORT_MAPPINGS: Record<string, string[]> = {
  'new york': ['JFK', 'EWR', 'LGA'],
  'nyc': ['JFK', 'EWR', 'LGA'],
  'los angeles': ['LAX'],
  'la': ['LAX'],
  'san francisco': ['SFO', 'OAK', 'SJC'],
  'sf': ['SFO', 'OAK', 'SJC'],
  'bay area': ['SFO', 'OAK', 'SJC'],
  'chicago': ['ORD', 'MDW'],
  'washington': ['DCA', 'IAD', 'BWI'],
  'dc': ['DCA', 'IAD', 'BWI'],
  'miami': ['MIA', 'FLL'],
  'boston': ['BOS'],
  'seattle': ['SEA'],
  'dallas': ['DFW', 'DAL'],
  'houston': ['IAH', 'HOU'],
  'paris': ['CDG', 'ORY'],
  'london': ['LHR', 'LGW', 'STN', 'LTN'],
  'tokyo': ['NRT', 'HND'],
  'rome': ['FCO', 'CIA'],
  'milan': ['MXP', 'LIN'],
  'bangkok': ['BKK', 'DMK'],
};

// Regional airport groups for "cheapest to Europe" type searches
const REGIONAL_GROUPS: Record<string, string[]> = {
  'europe': ['LHR', 'CDG', 'AMS', 'FCO', 'MAD', 'BCN', 'FRA', 'MUC', 'ZRH', 'VIE'],
  'asia': ['NRT', 'HND', 'ICN', 'PVG', 'HKG', 'SIN', 'BKK', 'DEL'],
  'caribbean': ['SJU', 'CUN', 'PUJ', 'MBJ', 'NAS', 'AUA'],
  'mexico': ['MEX', 'CUN', 'GDL', 'MTY', 'TIJ'],
  'south america': ['GRU', 'EZE', 'BOG', 'LIM', 'SCL'],
  'middle east': ['DXB', 'DOH', 'AUH', 'TLV', 'CAI'],
};

// Helper to check if a route exists
function isValidRoute(origin: string, destination: string): boolean {
  // Some routes that definitely don't have commercial flights
  const invalidPairs = [
    // Small regional airports to international destinations
    ['HVN', 'SJU'], // New Haven to San Juan - no direct service
  ];
  
  for (const [o, d] of invalidPairs) {
    if ((origin === o && destination === d) || (origin === d && destination === o)) {
      return false;
    }
  }
  
  return true;
}

// Helper function to search flights via SerpAPI
async function searchFlights(origin: string, destination: string, date: string, returnDate?: string) {
  const params: any = {
    engine: "google_flights",
    api_key: process.env.SERP_API_KEY,
    departure_id: origin,
    arrival_id: destination,
    outbound_date: date,
    currency: "USD",
    hl: "en",
    gl: "us",
    type: "2",
  };

  if (returnDate) {
    params.return_date = returnDate;
    params.type = "1"; // Round trip
  }

  const queryString = new URLSearchParams(params).toString();
  const response = await fetch(`https://serpapi.com/search.json?${queryString}`);
  const data = await response.json();

  if (data.error) {
    throw new Error(data.error);
  }

  return [...(data.best_flights || []), ...(data.other_flights || [])];
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { query, searchType, selectedOutbound, conversationHistory = [] } = body;

    console.log(`\n💬 User: "${query}"`);

    // CONVERSATIONAL SYSTEM PROMPT with context awareness
    const conversationContext = conversationHistory
      .slice(-6) // Last 3 exchanges
      .map((msg: any) => `${msg.role}: ${msg.content}`)
      .join('\n');

    const systemPrompt = `You are an intelligent flight search assistant with memory.

TODAY'S DATE: January 6, 2026

RECENT CONVERSATION:
${conversationContext || 'No prior context'}

USER'S LATEST MESSAGE: "${query}"

YOUR TASK: Analyze if you have enough information to execute a flight search.

REQUIRED INFO:
- Origin (airport code or city name)
- Destination (airport code, city name, OR REGION)
- Date (REQUIRED - must have this)
- Return date (if round trip)

SPECIAL HANDLING FOR REGIONAL SEARCHES:
When user asks for "cheapest to Europe" or "cheapest flight to Asia":
- Set "search_type": "multi_destination"
- Set "destination_region": "europe" or "asia" or "caribbean" etc.
- We will search MULTIPLE destinations and find the actual cheapest

AVAILABLE REGIONS:
- "europe" → Search: London, Paris, Amsterdam, Rome, Madrid, Barcelona, Frankfurt, Munich, Zurich, Vienna
- "asia" → Search: Tokyo, Seoul, Shanghai, Hong Kong, Singapore, Bangkok, Delhi
- "caribbean" → Search: San Juan, Cancun, Punta Cana, Montego Bay, Nassau, Aruba
- "mexico" → Search: Mexico City, Cancun, Guadalajara, Monterrey, Tijuana
- "south america" → Search: Sao Paulo, Buenos Aires, Bogota, Lima, Santiago
- "middle east" → Search: Dubai, Doha, Abu Dhabi, Tel Aviv, Cairo

DECISION LOGIC:
1. If ALL required info present → Return JSON with "action": "search"
2. If missing critical info → Return JSON with "action": "clarify"
3. If user asks for "cheapest to [region]" → Return "search_type": "multi_destination"

CITY → AIRPORTS MAPPING (use ALL airports for a city):
- "New York" → "JFK,EWR,LGA"
- "Paris" → "CDG,ORY"
- "London" → "LHR,LGW,STN,LTN"
- "San Francisco" → "SFO,OAK,SJC"
- "Washington" → "DCA,IAD,BWI"
- "Miami" → "MIA,FLL"
- "Chicago" → "ORD,MDW"
- "Dallas" → "DFW,DAL"
- "Houston" → "IAH,HOU"

AIRLINE MAPPINGS:
- "Spirit" → "NK"
- "Frontier" → "F9"
- "JetBlue" → "B6"
- "United" → "UA"
- "American" → "AA"
- "Delta" → "DL"
- "Alaska" → "AS"
- "Southwest" → "WN"
- "Allegiant" → "G4"
- "No budget airlines" → ["NK", "F9", "G4"]

RELATIVE DATES (based on Jan 6, 2026):
- "tomorrow" → 2026-01-07
- "next Friday" → 2026-01-10
- "this weekend" → 2026-01-11
- "next week" → 2026-01-13
- "week after" → add 7 days to last search date
- "next month" → February 2026

OUTPUT FORMAT:

STANDARD SEARCH (specific destination):
{
  "action": "search",
  "search_type": "standard",
  "origin": "JFK",
  "destination": "CDG,ORY",
  "date": "2026-02-05",
  "return_date": null,
  "exclude_airlines": []
}

MULTI-DESTINATION SEARCH (regional):
{
  "action": "search",
  "search_type": "multi_destination",
  "origin": "JFK",
  "destination_region": "europe",
  "date": "2026-02-05",
  "return_date": null,
  "exclude_airlines": []
}

CLARIFY MODE:
{
  "action": "clarify",
  "message": "When would you like to fly to Paris?"
}

EXAMPLES:

User: "Cheapest flight from JFK to Europe"
→ {"action": "search", "search_type": "multi_destination", "origin": "JFK", "destination_region": "europe", "date": "2026-02-15"}

User: "New York to Paris"
→ {"action": "clarify", "message": "When would you like to fly to Paris?"}

User: "New Haven to San Juan 2/5"
→ {"action": "search", "search_type": "standard", "origin": "HVN", "destination": "SJU", "date": "2026-02-05"}

Return ONLY valid JSON, no markdown.`;

    const msg = await anthropic.messages.create({
      model: "claude-sonnet-4-20250514", // Upgraded for better conversation
      max_tokens: 2000,
      system: systemPrompt,
      messages: [{ role: "user", content: query }],
    });

    const textBlock = msg.content[0];
    if (textBlock.type !== 'text') throw new Error('Invalid AI response');
    
    // Clean JSON response
    let responseText = textBlock.text.trim();
    responseText = responseText.replace(/```json\s*/g, '').replace(/```\s*/g, '');
    const jsonStart = responseText.indexOf('{');
    const jsonEnd = responseText.lastIndexOf('}');
    if (jsonStart !== -1 && jsonEnd !== -1) {
      responseText = responseText.substring(jsonStart, jsonEnd + 1);
    }

    const parsedQuery = JSON.parse(responseText);
    console.log("Claude decision:", JSON.stringify(parsedQuery, null, 2));

    // CLARIFICATION MODE: Ask user for more info
    if (parsedQuery.action === 'clarify') {
      return NextResponse.json({
        mode: 'clarify',
        message: parsedQuery.message,
        context: parsedQuery.context || {},
      });
    }

    // SEARCH MODE: Execute flight search
    const criteria = parsedQuery;

    // DATE FALLBACK: If user didn't specify a date, default to tomorrow
    if (!criteria.date) {
      const tomorrow = new Date();
      tomorrow.setDate(tomorrow.getDate() + 1);
      criteria.date = tomorrow.toISOString().split('T')[0];
    }

    // HANDLE MULTI-DESTINATION SEARCHES (e.g., "cheapest to Europe")
    if (criteria.search_type === 'multi_destination' && criteria.destination_region) {
      const region = criteria.destination_region.toLowerCase();
      const destinations = REGIONAL_GROUPS[region];
      
      if (!destinations) {
        return NextResponse.json({
          mode: 'error',
          message: `I don't recognize the region "${region}". Try: Europe, Asia, Caribbean, Mexico, South America, or Middle East.`,
          results: [],
        });
      }

      console.log(`🌍 Multi-destination search: ${criteria.origin} → ${region} (${destinations.length} airports)`);

      try {
        // Search all destinations in parallel
        const searchPromises = destinations.map(dest => 
          searchFlights(criteria.origin, dest, criteria.date).catch(err => {
            console.log(`Failed to search ${dest}:`, err.message);
            return []; // Return empty array on failure
          })
        );

        const allResults = await Promise.all(searchPromises);
        
        // Flatten and combine all flights
        const allFlights = allResults.flat();

        if (allFlights.length === 0) {
          return NextResponse.json({
            mode: 'search',
            message: `No flights found from ${criteria.origin} to ${region}. Try different dates.`,
            results: [],
          });
        }

        // Process and sort by price
        const excludedAirlines = criteria.exclude_airlines || [];
        const processedFlights = allFlights
          .map((f: any) => {
            const leg = f.flights[0];
            const lastLeg = f.flights[f.flights.length - 1];
            const airlineCode = leg.airline_logo?.match(/airlines\/(\w{2})/)?.[1] || '';
            
            // Skip excluded airlines
            if (airlineCode && excludedAirlines.includes(airlineCode)) {
              return null;
            }

            return {
              airline: leg.airline,
              airline_code: airlineCode,
              price: f.price,
              duration: f.total_duration,
              stops: f.layovers?.length || 0,
              departure_time: leg.departure_airport?.time,
              arrival_time: lastLeg.arrival_airport?.time,
              departure_airport: leg.departure_airport?.id || criteria.origin,
              arrival_airport: lastLeg.arrival_airport?.id || '',
              booking_token: f.booking_token,
              departure_id: leg.departure_airport?.id || criteria.origin,
              arrival_id: lastLeg.arrival_airport?.id || '',
              outbound_date: criteria.date,
              aircraft: leg.airplane,
            };
          })
          .filter((r: any) => r !== null)
          .sort((a: any, b: any) => a.price - b.price); // Sort by price

        console.log(`✅ Found ${processedFlights.length} flights across ${destinations.length} destinations`);

        return NextResponse.json({
          mode: 'search',
          message: `Cheapest flights from ${criteria.origin} to ${region}\nSearched ${destinations.length} destinations`,
          results: processedFlights.slice(0, 15), // Show top 15 cheapest
          isRoundTrip: false,
        });

      } catch (error: any) {
        console.error('Multi-destination search error:', error);
        return NextResponse.json({
          mode: 'error',
          message: 'Failed to search multiple destinations. Please try again.',
          results: [],
        });
      }
    }

    // STANDARD SEARCH: Single destination
    if (!criteria.origin || !criteria.destination) {
      return NextResponse.json({
        mode: 'clarify',
        message: "I need to know where you're flying from and to. For example: 'JFK to Miami on Feb 5'",
      });
    }

    // Check if route is known to not exist
    const origins = criteria.origin.split(',');
    const destinations = criteria.destination.split(',');
    
    let validRouteExists = false;
    for (const origin of origins) {
      for (const dest of destinations) {
        if (isValidRoute(origin.trim(), dest.trim())) {
          validRouteExists = true;
          break;
        }
      }
      if (validRouteExists) break;
    }

    if (!validRouteExists) {
      return NextResponse.json({
        mode: 'error',
        message: `No commercial flights exist between ${criteria.origin} and ${criteria.destination}. Try searching from a major airport like JFK, EWR, or BOS.`,
        results: [],
      });
    }

    // PARAMETER CONSTRUCTION for standard search
    const params: any = {
      engine: "google_flights",
      api_key: process.env.SERP_API_KEY,
      departure_id: criteria.origin,
      arrival_id: criteria.destination,
      outbound_date: criteria.date,
      currency: "USD",
      hl: "en",
      gl: "us",
      type: "2", // Force One-Way search
    };

    // Handle Inclusion vs. Exclusion
    if (criteria.include_airlines?.length > 0) {
      params.airline = criteria.include_airlines.join(',');
    } else if (criteria.exclude_airlines?.length > 0) {
      params.exclude_airlines = criteria.exclude_airlines.join(',');
    }

    // Stops Mapping
    if (criteria.stops === 0) {
      params.stops = "1";
    } else if (criteria.stops === 1) {
      params.stops = "2";
    }

    // Cabin Mapping
    const cabinMap: any = { "economy": "1", "premium_economy": "2", "business": "3", "first": "4" };
    if (criteria.cabin && cabinMap[criteria.cabin]) {
      params.seat_class = cabinMap[criteria.cabin];
    }

    // Return Leg Configuration
    if (searchType === 'return' && selectedOutbound) {
      params.departure_id = criteria.destination;
      params.arrival_id = criteria.origin;
      params.outbound_date = criteria.return_date;
    }

    console.log(`🔍 Searching: ${params.departure_id} → ${params.arrival_id} on ${params.outbound_date}`);

    // FETCH with error handling
    let serpData;
    try {
      const queryString = new URLSearchParams(params).toString();
      const serpResponse = await fetch(`https://serpapi.com/search.json?${queryString}`);
      serpData = await serpResponse.json();
    } catch (error: any) {
      console.error("SerpAPI fetch error:", error);
      return NextResponse.json({ 
        mode: 'error',
        message: 'Failed to connect to flight search. Please try again.', 
        results: [] 
      });
    }

    if (serpData.error) {
      console.error("SerpApi Error:", serpData.error);
      return NextResponse.json({ 
        mode: 'error',
        message: `Search error: ${serpData.error}. Try different dates or airports.`, 
        results: [] 
      });
    }

    const flights = serpData.best_flights || serpData.other_flights || [];

    if (flights.length === 0) {
      // Provide helpful suggestions
      const origins = params.departure_id.split(',');
      const dests = params.arrival_id.split(',');
      
      let suggestion = '';
      if (origins.length > 1 || dests.length > 1) {
        suggestion = `\n\nSearched multiple airports but found no flights.`;
      }

      return NextResponse.json({
        mode: 'search',
        message: `No flights found for ${params.departure_id} → ${params.arrival_id} on ${params.outbound_date}.${suggestion}\n\nTry: Different dates, nearby airports, or adding connections.`,
        results: [],
      });
    }

    // TRANSFORM with airport codes clearly displayed
    const excludedAirlines = criteria.exclude_airlines || [];
    const results = flights.map((flight: any) => {
      const leg = flight.flights[0];
      const lastLeg = flight.flights[flight.flights.length - 1];
      const airlineCode = leg.airline_logo?.match(/airlines\/(\w{2})/)?.[1] || '';
      
      // Skip excluded airlines
      if (airlineCode && excludedAirlines.includes(airlineCode)) {
        return null;
      }

      return {
        airline: leg.airline,
        airline_code: airlineCode,
        price: flight.price,
        duration: flight.total_duration,
        stops: flight.layovers?.length || 0,
        departure_time: leg.departure_airport?.time,
        arrival_time: lastLeg.arrival_airport?.time,
        // Show specific airport codes
        departure_airport: leg.departure_airport?.id || params.departure_id.split(',')[0],
        arrival_airport: lastLeg.arrival_airport?.id || params.arrival_id.split(',')[0],
        booking_token: flight.booking_token,
        departure_id: leg.departure_airport?.id || params.departure_id.split(',')[0],
        arrival_id: lastLeg.arrival_airport?.id || params.arrival_id.split(',')[0],
        outbound_date: params.outbound_date,
        aircraft: leg.airplane,
      };
    })
    .filter((r: any) => r !== null)
    .sort((a: any, b: any) => a.price - b.price);

    console.log(`✅ Returning ${results.length} flights\n`);

    if (results.length === 0) {
      const excluded = excludedAirlines.join(', ');
      return NextResponse.json({
        mode: 'search',
        message: `No flights match your criteria.\n\n${excluded ? `Excluded airlines: ${excluded}\n` : ''}Try fewer restrictions or different airports.`,
        results: [],
      });
    }

    // Build message
    let message = '';
    if (searchType === 'return') {
      message = `Return flights\nDate: ${params.outbound_date}\n\nSelect your return flight:`;
    } else if (criteria.return_date) {
      message = `Outbound flights\nDate: ${params.outbound_date}\n\nSelect your outbound flight:`;
    } else {
      message = `Found ${results.length} flights on ${params.outbound_date}`;
    }

    return NextResponse.json({
      mode: 'search',
      message,
      results: results.slice(0, 10),
      isRoundTrip: !!criteria.return_date && searchType !== 'return',
      searchCriteria: criteria,
    });

  } catch (error: any) {
    console.error("Search API Error:", error);
    return NextResponse.json({ 
      mode: 'error',
      message: error.message || 'An unexpected error occurred. Please try again.',
      results: []
    }, { status: 500 });
  }
}
