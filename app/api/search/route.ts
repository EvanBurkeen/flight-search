import { NextResponse } from 'next/server';
import Anthropic from '@anthropic-ai/sdk';

const anthropic = new Anthropic({
  apiKey: process.env.ANTHROPIC_API_KEY || '',
});

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
    type: returnDate ? "1" : "2", // 1 = round trip, 2 = one-way
  };

  if (returnDate) {
    params.return_date = returnDate;
  }

  const queryString = new URLSearchParams(params).toString();
  const response = await fetch(`https://serpapi.com/search.json?${queryString}`);
  const data = await response.json();

  if (data.error) {
    throw new Error(data.error);
  }

  return [...(data.best_flights || []), ...(data.other_flights || [])];
}

// Helper to search multiple destinations and find cheapest
async function searchMultipleDestinations(
  origin: string,
  destinations: string[],
  date: string,
  returnDate?: string,
  maxToSearch: number = 15 // Default to 15 for speed
): Promise<{ destination: string; flights: any[]; price: number }[]> {
  const results = [];
  const destinationsToSearch = destinations.slice(0, maxToSearch);
  
  console.log(`🌍 Searching ${destinationsToSearch.length} destinations...`);
  
  for (const dest of destinationsToSearch) {
    try {
      console.log(`🔍 Checking ${origin} → ${dest}...`);
      const flights = await searchFlights(origin, dest, date, returnDate);
      
      if (flights && flights.length > 0) {
        const cheapestPrice = Math.min(...flights.map((f: any) => f.price || Infinity));
        results.push({
          destination: dest,
          flights,
          price: cheapestPrice
        });
        console.log(`  ✓ ${dest}: $${cheapestPrice}`);
      }
    } catch (error) {
      console.log(`  ✗ ${dest}: No flights`);
    }
  }
  
  if (results.length === 0) {
    console.log('❌ No flights found across any destination');
  } else {
    console.log(`✅ Found flights to ${results.length} destinations`);
  }
  
  // Sort by price
  return results.sort((a, b) => a.price - b.price);
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { query, searchType, selectedOutbound, conversationHistory = [] } = body;

    console.log(`\n💬 User: "${query}"`);

    // Build conversation context
    const conversationContext = conversationHistory
      .slice(-6)
      .map((msg: any) => `${msg.role}: ${msg.content}`)
      .join('\n');

    const systemPrompt = `You are an intelligent flight search assistant with deep knowledge of airports and geography.

TODAY'S DATE: January 6, 2026

CONVERSATION:
${conversationContext || 'No prior context'}

USER: "${query}"

ROUND TRIP DETECTION:
Look for: "round trip", "return", "coming back", two dates (2/5-2/8), date ranges
If detected → set BOTH "date" AND "return_date"

GEOGRAPHIC SEARCH INTELLIGENCE:
When user asks about REGIONS (not specific cities), search MULTIPLE major airports:

EUROPE (50+ airports - Tier 2 coverage):
Primary Hubs: CDG, ORY (Paris), LHR, LGW, STN (London), AMS (Amsterdam), FRA (Frankfurt), MUC (Munich), MAD, BCN (Spain), FCO, MXP (Italy), VIE (Vienna), ZRH, GVA (Switzerland), CPH (Copenhagen), ARN (Stockholm), OSL (Oslo), HEL (Helsinki), DUB (Dublin), BRU (Brussels)
Secondary Hubs: ATH (Athens), LIS, OPO (Portugal), BUD (Budapest), PRG (Prague), WAW (Warsaw), KRK (Krakow), IST (Istanbul), BER, HAM (Germany), VCE, NAP, BGY, BLQ (Italy), NCE, LYS, MRS (France), BIO, SVQ, AGP, VLC (Spain), EDI, MAN, BHX (UK), BRN (Switzerland), SOF (Bulgaria), OTP (Romania), RIX (Riga), TLL (Tallinn), VNO (Vilnius)

LATIN AMERICA (40+ airports - Tier 2 coverage):
Primary Hubs: GRU, GIG (Brazil - São Paulo, Rio), MEX (Mexico City), BOG (Bogotá), LIM (Lima), SCL (Santiago), EZE (Buenos Aires), PTY (Panama City), UIO (Quito), CUN (Cancún)
Secondary Hubs: BSB, CNF, FOR, SSA, POA, REC (Brazil), GDL, MTY, TIJ (Mexico), MDE, CTG, CLO (Colombia), CUZ (Peru), GYE (Ecuador), MVD (Montevideo), ASU (Asunción), SJO (San José), SDQ (Santo Domingo), HAV (Havana), SJU (San Juan), CCS (Caracas), MIA, FLL, IAH, DFW (US gateways to LATAM)

ASIA (50+ airports - Tier 2 coverage):
Primary Hubs: NRT, HND (Tokyo), ICN (Seoul), PVG, PEK, CAN, SZX (China), HKG (Hong Kong), TPE (Taipei), SIN (Singapore), BKK, DMK (Bangkok), KUL (Kuala Lumpur), CGK (Jakarta), MNL (Manila), HAN, SGN (Vietnam), DXB, AUH (UAE), DOH (Doha), KWI (Kuwait)
Secondary Hubs: DEL, BOM, BLR, MAA, HYD, CCU (India), CMB (Sri Lanka), KTM (Kathmandu), DAC (Dhaka), RGN (Yangon), PNH (Phnom Penh), VTE (Vientiane), USM (Samui), CNX (Chiang Mai), DPS (Bali), SUB (Surabaya), IST (Turkey/Asia gateway), TLV (Tel Aviv), AMM (Amman), MCT (Muscat), BAH (Bahrain), ULN (Ulaanbaatar), TAS (Tashkent), ALA (Almaty)

For broad queries like "cheapest to Europe" or "anywhere in Asia":
1. Search 10-15 PRIMARY hubs for the region (balance speed vs coverage)
2. Return the cheapest option found
3. TELL THE USER which airports you checked and which was cheapest
4. Offer to check MORE if they want (secondary hubs)

Example responses:
- "I checked Paris CDG, London LHR, Amsterdam AMS... [10 total]. Paris was cheapest at $520."
- "Searched Tokyo HND, Seoul ICN, Bangkok BKK... [12 total]. Bangkok had the best deal at $680. Want me to check more Asian cities?"

REGIONAL QUERY EXAMPLES:

"Cheapest to Europe" → destination: ["CDG", "LHR", "AMS", "FRA", "MAD", "BCN", "FCO", "MXP", "VIE", "ZRH", "CPH", "DUB", "BRU", "ATH", "LIS"]

"Anywhere in Asia" → destination: ["HND", "ICN", "SIN", "BKK", "HKG", "TPE", "KUL", "PVG", "DEL", "BOM", "DXB", "DOH", "MNL", "CGK", "HAN"]

"Latin America or South America" → destination: ["GRU", "GIG", "MEX", "BOG", "LIM", "SCL", "EZE", "PTY", "UIO", "CUN", "MDE", "BSB", "GDL", "MVD", "SJO"]

"Southeast Asia" → destination: ["BKK", "SIN", "KUL", "CGK", "MNL", "HAN", "SGN", "PNH", "RGN", "DPS"]

"Mediterranean" → destination: ["FCO", "ATH", "BCN", "MAD", "LIS", "IST", "TLV", "VCE", "NAP", "NCE"]

CITY MAPPINGS (for specific cities):
New York → JFK,EWR,LGA | Paris → CDG,ORY | London → LHR,LGW,STN,LTN
San Francisco → SFO,OAK,SJC | Washington → DCA,IAD,BWI | Miami → MIA,FLL
Los Angeles → LAX,BUR,ONT | Chicago → ORD,MDW | Boston → BOS
Seattle → SEA | Denver → DEN | Atlanta → ATL | Dallas → DFW

CONVERSATIONAL INTELLIGENCE:
- If user asks about your search process (e.g., "what airports did you check?" "why CDG?" "what about other cities?"), use CLARIFY mode to explain
- Look at the conversation history - if you just searched multiple airports, reference those in your response
- If user is dissatisfied with results, offer to search more airports or different dates
- Be helpful and explanatory, not just transactional
- If search fails, explain why and suggest alternatives
- ALWAYS respond conversationally to questions about your process - never just error out

OUTPUT MODES:

1) SEARCH - Perform actual flight search:
{
  "action": "search",
  "search_type": "multi_airport" (for regions) OR "standard" (for specific routes),
  "origin": "JFK",
  "destination": "CDG",  // or array ["CDG", "LHR", "AMS"] for multi-airport
  "date": "2026-02-05",
  "return_date": "2026-02-08" (or null for one-way),
  "exclude_airlines": [],
  "checked_airports": ["CDG", "LHR", "AMS", "FCO", "MAD"]  // for explaining to user
}

2) CLARIFY - Need more info OR respond conversationally:
{
  "action": "clarify",
  "message": "I searched Paris CDG, London LHR, Amsterdam AMS, Rome FCO, and Madrid MAD. Paris CDG had the cheapest option at $450. Would you like me to check other European cities like Frankfurt, Munich, or Barcelona?"
}

3) ERROR - When something is unclear:
{
  "action": "error",
  "message": "I need more information about..."
}

KEY RULES:
- For "cheapest to [region]", search multiple airports and find the best
- Always tell the user which airports you checked
- Respond conversationally to follow-up questions
- Be helpful and transparent about your search process

Return ONLY valid JSON, no markdown.`;

    const msg = await anthropic.messages.create({
      model: "claude-sonnet-4-20250514",
      max_tokens: 2000,
      system: systemPrompt,
      messages: [{ role: "user", content: query }],
    });

    const textBlock = msg.content[0];
    if (textBlock.type !== 'text') throw new Error('Invalid AI response');

    let responseText = textBlock.text.trim();
    responseText = responseText.replace(/```json\s*/g, '').replace(/```\s*/g, '');
    const jsonStart = responseText.indexOf('{');
    const jsonEnd = responseText.lastIndexOf('}');
    if (jsonStart !== -1 && jsonEnd !== -1) {
      responseText = responseText.substring(jsonStart, jsonEnd + 1);
    }

    const parsedQuery = JSON.parse(responseText);
    console.log("Claude decision:", JSON.stringify(parsedQuery, null, 2));

    // CLARIFICATION MODE
    if (parsedQuery.action === 'clarify') {
      return NextResponse.json({
        mode: 'clarify',
        message: parsedQuery.message,
        context: parsedQuery.context || {},
      });
    }

    // SEARCH MODE
    const criteria = parsedQuery;

    if (!criteria.date) {
      const tomorrow = new Date();
      tomorrow.setDate(tomorrow.getDate() + 1);
      criteria.date = tomorrow.toISOString().split('T')[0];
    }

    // ROUND TRIP SEARCH
    if (criteria.return_date && searchType !== 'return_selection') {
      console.log(`🔄 Round trip: ${criteria.origin} → ${criteria.destination} (${criteria.date} to ${criteria.return_date})`);

      try {
        let packages;
        let searchedDestinations: string[] = [];
        
        // Check if destination is array for multi-airport search
        const destinations = Array.isArray(criteria.destination) 
          ? criteria.destination 
          : [criteria.destination];
        
        if (destinations.length > 1) {
          // MULTI-AIRPORT SEARCH
          console.log(`🌍 Multi-airport search: checking ${destinations.length} destinations`);
          const multiResults = await searchMultipleDestinations(
            criteria.origin,
            destinations,
            criteria.date,
            criteria.return_date
          );
          
          if (multiResults.length === 0) {
            return NextResponse.json({
              mode: 'clarify',
              message: `I checked ${destinations.join(', ')} but couldn't find any flights. Would you like to try different dates or destinations?`,
            });
          }
          
          // MERGE packages from ALL destinations and sort by price
          const allPackages: any[] = [];
          const destinationPrices: string[] = [];
          
          for (const result of multiResults) {
            destinationPrices.push(`${result.destination} ($${result.price})`);
            
            // Add destination info to each package
            result.flights.forEach((pkg: any) => {
              allPackages.push({
                ...pkg,
                _destination: result.destination
              });
            });
          }
          
          // Sort all packages by price
          packages = allPackages.sort((a, b) => (a.price || 0) - (b.price || 0));
          searchedDestinations = destinationPrices;
          
          console.log(`✅ Found ${packages.length} total packages across ${multiResults.length} destinations`);
          console.log(`💰 Price range: $${packages[0]?.price} - $${packages[packages.length - 1]?.price}`);
        } else {
          // STANDARD SINGLE DESTINATION SEARCH
          packages = await searchFlights(
            criteria.origin,
            criteria.destination,
            criteria.date,
            criteria.return_date
          );
        }

        if (!packages || packages.length === 0) {
          return NextResponse.json({
            mode: 'search',
            message: `No round-trip flights found for ${criteria.origin} → ${criteria.destination}.`,
            results: [],
          });
        }

        console.log(`📦 Found ${packages.length} round-trip packages`);

        // Transform round-trip packages into bookable results with comprehensive null checks
        const results = packages
          .map((pkg: any) => {
            // Validate package has required data
            if (!pkg.flights || pkg.flights.length === 0) {
              console.warn('Package missing flights:', pkg);
              return null;
            }

            const outboundFlight = pkg.flights[0]; // First flight leg
            const lastOutboundLeg = pkg.flights[pkg.flights.length - 1]; // Last leg to get final destination
            
            // Validate we have departure_token (required for return flight lookup)
            if (!pkg.departure_token) {
              console.warn('Package missing departure_token:', pkg);
              return null;
            }

            return {
              airline: outboundFlight?.airline || 'Unknown',
              airline_code: outboundFlight?.airline_logo?.match(/airlines\/(\w{2})/)?.[1] || '',
              price: pkg.price || 0,
              duration: pkg.total_duration || outboundFlight?.duration || 0,
              stops: (pkg.layovers?.length || 0),
              layovers: pkg.layovers || [],
              departure_time: outboundFlight?.departure_airport?.time || '',
              arrival_time: lastOutboundLeg?.arrival_airport?.time || '',
              departure_airport: outboundFlight?.departure_airport?.id || criteria.origin,
              arrival_airport: lastOutboundLeg?.arrival_airport?.id || pkg._destination || 'Unknown',
              booking_token: pkg.departure_token, // Round trips use departure_token
              departure_id: criteria.origin, // Use original search criteria for return flight API
              arrival_id: lastOutboundLeg?.arrival_airport?.id || pkg._destination || 'Unknown', // Use actual destination
              outbound_date: criteria.date,
              return_date: criteria.return_date,
              is_round_trip: true,
              aircraft: outboundFlight?.airplane || '',
            };
          })
          .filter((result): result is NonNullable<typeof result> => result !== null) // Remove null entries
          .sort((a, b) => a.price - b.price);

        console.log(`✅ Returning ${results.length} round-trip packages`);

        // Build message - for multi-airport, don't specify single destination
        let message;
        if (searchedDestinations.length > 1) {
          message = `Round trip flights: ${criteria.origin} → Europe\nOutbound: ${criteria.date} | Return: ${criteria.return_date}\n\n🔍 Searched ${searchedDestinations.length} airports: ${searchedDestinations.slice(0, 8).join(', ')}${searchedDestinations.length > 8 ? ` +${searchedDestinations.length - 8} more` : ''}`;
        } else {
          message = `Round trip flights\n${criteria.origin} → ${criteria.destination}\nOutbound: ${criteria.date} | Return: ${criteria.return_date}`;
        }
        
        message += `\n\nComplete packages (price includes return):`;

        return NextResponse.json({
          mode: 'search',
          message,
          results: results.slice(0, 10),
          searchCriteria: criteria,
          searchedAirports: searchedDestinations.length > 0 ? searchedDestinations : undefined,
        });

      } catch (error: any) {
        console.error('Round trip search error:', error);
        return NextResponse.json({
          mode: 'error',
          message: `Search failed: ${error.message}`,
          results: [],
        });
      }
    }

    // ONE-WAY SEARCH
    console.log(`🔍 One-way: ${criteria.origin} → ${criteria.destination} on ${criteria.date}`);

    try {
      let flights;
      let searchedDestinations: string[] = [];
      
      // Check if destination is array for multi-airport search
      const destinations = Array.isArray(criteria.destination) 
        ? criteria.destination 
        : [criteria.destination];
      
      if (destinations.length > 1) {
        // MULTI-AIRPORT SEARCH FOR ONE-WAY
        console.log(`🌍 Multi-airport one-way search: checking ${destinations.length} destinations`);
        const multiResults = await searchMultipleDestinations(
          criteria.origin,
          destinations,
          criteria.date
        );
        
        if (multiResults.length === 0) {
          return NextResponse.json({
            mode: 'clarify',
            message: `I checked ${destinations.join(', ')} but couldn't find any flights. Would you like to try different dates or destinations?`,
          });
        }
        
        // MERGE flights from ALL destinations and sort by price
        const allFlights: any[] = [];
        const destinationPrices: string[] = [];
        
        for (const result of multiResults) {
          destinationPrices.push(`${result.destination} ($${result.price})`);
          
          // Add destination info to each flight
          result.flights.forEach((flight: any) => {
            allFlights.push({
              ...flight,
              _destination: result.destination
            });
          });
        }
        
        // Sort all flights by price
        flights = allFlights.sort((a, b) => (a.price || 0) - (b.price || 0));
        searchedDestinations = destinationPrices;
        
        console.log(`✅ Found ${flights.length} total flights across ${multiResults.length} destinations`);
        console.log(`💰 Price range: $${flights[0]?.price} - $${flights[flights.length - 1]?.price}`);
      } else {
        // STANDARD SINGLE DESTINATION SEARCH
        flights = await searchFlights(criteria.origin, criteria.destination, criteria.date);
      }

      if (!flights || flights.length === 0) {
        return NextResponse.json({
          mode: 'search',
          message: `No flights found for ${criteria.origin} → ${criteria.destination}.`,
          results: [],
        });
      }

      const results = flights.map((flight: any) => {
        const leg = flight.flights[0];
        const lastLeg = flight.flights[flight.flights.length - 1];

        return {
          airline: leg.airline,
          airline_code: leg.airline_logo?.match(/airlines\/(\w{2})/)?.[1] || '',
          price: flight.price,
          duration: flight.total_duration,
          stops: flight.layovers?.length || 0,
          layovers: flight.layovers || [],
          departure_time: leg.departure_airport?.time,
          arrival_time: lastLeg.arrival_airport?.time,
          departure_airport: leg.departure_airport?.id || criteria.origin,
          arrival_airport: lastLeg.arrival_airport?.id || flight._destination || 'Unknown',
          booking_token: flight.booking_token,
          departure_id: leg.departure_airport?.id || criteria.origin,
          arrival_id: lastLeg.arrival_airport?.id || flight._destination || 'Unknown',
          outbound_date: criteria.date,
          aircraft: leg.airplane,
        };
      })
      .sort((a, b) => a.price - b.price);

      console.log(`✅ Returning ${results.length} one-way flights`);

      // Build message - for multi-airport, don't specify single destination
      let message;
      if (searchedDestinations.length > 1) {
        message = `One-way flights: ${criteria.origin} → Europe on ${criteria.date}\n\n🔍 Searched ${searchedDestinations.length} airports: ${searchedDestinations.slice(0, 8).join(', ')}${searchedDestinations.length > 8 ? ` +${searchedDestinations.length - 8} more` : ''}`;
      } else {
        message = `One-way flights: ${criteria.origin} → ${criteria.destination} on ${criteria.date}`;
      }

      return NextResponse.json({
        mode: 'search',
        message,
        results: results.slice(0, 10),
        searchCriteria: criteria,
        searchedAirports: searchedDestinations.length > 0 ? searchedDestinations : undefined,
      });

    } catch (error: any) {
      return NextResponse.json({
        mode: 'error',
        message: `Search failed: ${error.message}`,
        results: [],
      });
    }

  } catch (error: any) {
    console.error("Search API Error:", error);
    return NextResponse.json({
      mode: 'error',
      message: error.message || 'An unexpected error occurred.',
      results: []
    }, { status: 500 });
  }
}
