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

    const systemPrompt = `You are an intelligent flight search assistant.

TODAY'S DATE: January 6, 2026

CONVERSATION:
${conversationContext || 'No prior context'}

USER: "${query}"

ROUND TRIP DETECTION:
Look for: "round trip", "return", "coming back", two dates (2/5-2/8), date ranges
If detected → set BOTH "date" AND "return_date"

CITY MAPPINGS:
New York → JFK,EWR,LGA | Paris → CDG,ORY | London → LHR,LGW,STN,LTN
San Francisco → SFO,OAK,SJC | Washington → DCA,IAD,BWI | Miami → MIA,FLL

OUTPUT:

SEARCH:
{
  "action": "search",
  "search_type": "standard",
  "origin": "JFK",
  "destination": "CDG",
  "date": "2026-02-05",
  "return_date": "2026-02-08" (or null for one-way),
  "exclude_airlines": []
}

CLARIFY:
{
  "action": "clarify",
  "message": "When would you like to fly?"
}

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
        const packages = await searchFlights(
          criteria.origin,
          criteria.destination,
          criteria.date,
          criteria.return_date
        );

        if (!packages || packages.length === 0) {
          return NextResponse.json({
            mode: 'search',
            message: `No round-trip flights found for ${criteria.origin} → ${criteria.destination}.`,
            results: [],
          });
        }

        console.log(`📦 Found ${packages.length} round-trip packages`);

        // Transform round-trip packages into bookable results
        const results = packages.map((pkg: any) => {
          const outboundFlight = pkg.flights[0]; // First flight (outbound)
          
          return {
            airline: outboundFlight.airline,
            airline_code: outboundFlight.airline_logo?.match(/airlines\/(\w{2})/)?.[1] || '',
            price: pkg.price,
            duration: pkg.total_duration || outboundFlight.duration,
            stops: pkg.flights.length - 1,
            departure_time: outboundFlight.departure_airport?.time,
            arrival_time: outboundFlight.arrival_airport?.time,
            departure_airport: outboundFlight.departure_airport?.id || criteria.origin,
            arrival_airport: outboundFlight.arrival_airport?.id || criteria.destination,
            booking_token: pkg.booking_token, // Use booking_token for direct round trip booking
            departure_id: outboundFlight.departure_airport?.id || criteria.origin,
            arrival_id: outboundFlight.arrival_airport?.id || criteria.destination,
            outbound_date: criteria.date,
            return_date: criteria.return_date,
            is_round_trip: true,
            aircraft: outboundFlight.airplane,
          };
        })
        .sort((a, b) => a.price - b.price);

        console.log(`✅ Returning ${results.length} round-trip packages`);

        return NextResponse.json({
          mode: 'search',
          message: `Round trip flights\n${criteria.origin} → ${criteria.destination}\nOutbound: ${criteria.date} | Return: ${criteria.return_date}\n\nComplete packages (price includes return):`,
          results: results.slice(0, 10),
          searchCriteria: criteria,
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
      const flights = await searchFlights(criteria.origin, criteria.destination, criteria.date);

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
          departure_time: leg.departure_airport?.time,
          arrival_time: lastLeg.arrival_airport?.time,
          departure_airport: leg.departure_airport?.id || criteria.origin,
          arrival_airport: lastLeg.arrival_airport?.id || criteria.destination,
          booking_token: flight.booking_token,
          departure_id: leg.departure_airport?.id || criteria.origin,
          arrival_id: lastLeg.arrival_airport?.id || criteria.destination,
          outbound_date: criteria.date,
          aircraft: leg.airplane,
        };
      })
      .sort((a, b) => a.price - b.price);

      console.log(`✅ Returning ${results.length} one-way flights`);

      return NextResponse.json({
        mode: 'search',
        message: `Found ${results.length} flights on ${criteria.date}`,
        results: results.slice(0, 10),
        searchCriteria: criteria,
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
