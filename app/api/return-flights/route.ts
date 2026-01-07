import { NextResponse } from 'next/server';

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { departure_token, departure_id, arrival_id, outbound_date, return_date } = body;

    if (!departure_token || !departure_id || !arrival_id || !outbound_date || !return_date) {
      console.error('Missing parameters:', { departure_token: !!departure_token, departure_id, arrival_id, outbound_date, return_date });
      return NextResponse.json({ 
        error: 'departure_token, departure_id, arrival_id, outbound_date, and return_date are required' 
      }, { status: 400 });
    }

    const apiKey = process.env.SERP_API_KEY;

    console.log('🔄 Fetching return flights with departure_token');

    // Call SerpAPI with ALL required parameters
    const params = new URLSearchParams({
      engine: "google_flights",
      departure_token,
      departure_id,
      arrival_id,
      outbound_date,
      return_date,
      api_key: apiKey || '',
      currency: "USD",
      hl: "en",
      gl: "us",
    });

    const response = await fetch(`https://serpapi.com/search.json?${params.toString()}`);
    const data = await response.json();

    if (data.error) {
      console.error("SerpAPI Error:", data.error);
      return NextResponse.json({ error: data.error }, { status: 500 });
    }

    const returnFlights = [
      ...(data.best_flights || []),
      ...(data.other_flights || [])
    ];

    console.log(`✅ Found ${returnFlights.length} return flight options`);

    if (returnFlights.length === 0) {
      return NextResponse.json({
        mode: 'return_selection',
        results: [],
        message: 'No return flights available for this route.',
      });
    }

    // Transform return flights
    const results = returnFlights.map((flight: any) => {
      const firstLeg = flight.flights[0];
      const lastLeg = flight.flights[flight.flights.length - 1];

      return {
        airline: firstLeg?.airline || 'Unknown',
        airline_code: firstLeg?.airline_logo?.match(/airlines\/(\w{2})/)?.[1] || '',
        price: flight.price || 0,
        duration: flight.total_duration || 0,
        stops: (flight.layovers?.length || 0),
        layovers: flight.layovers || [],
        departure_time: firstLeg?.departure_airport?.time || '',
        arrival_time: lastLeg?.arrival_airport?.time || '',
        departure_airport: firstLeg?.departure_airport?.id || arrival_id,
        arrival_airport: lastLeg?.arrival_airport?.id || departure_id,
        booking_token: flight.booking_token || '',
        is_round_trip: true,
        aircraft: firstLeg?.airplane || '',
      };
    }).filter(f => f.booking_token); // Only include flights with valid booking tokens

    return NextResponse.json({
      mode: 'return_selection',
      results,
      message: `Found ${results.length} return flight options. Prices shown are total round trip.`,
    });

  } catch (error: any) {
    console.error("Return flights error:", error);
    return NextResponse.json({
      error: 'Failed to fetch return flights',
      details: error.message
    }, { status: 500 });
  }
}
