import { NextResponse } from 'next/server';

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { departure_token, departure_id, arrival_id } = body;

    if (!departure_token || !departure_id || !arrival_id) {
      return NextResponse.json({ 
        error: 'departure_token, departure_id, and arrival_id are required' 
      }, { status: 400 });
    }

    const apiKey = process.env.SERP_API_KEY;

    console.log('🔄 Fetching return flights with departure_token');

    // Call SerpAPI with departure_token AND required IDs
    const params = new URLSearchParams({
      engine: "google_flights",
      departure_token,
      departure_id,
      arrival_id,
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

    // Transform return flights
    const results = returnFlights.map((flight: any) => {
      const firstLeg = flight.flights[0];
      const lastLeg = flight.flights[flight.flights.length - 1];

      return {
        airline: firstLeg.airline,
        airline_code: firstLeg.airline_logo?.match(/airlines\/(\w{2})/)?.[1] || '',
        price: flight.price, // This is the TOTAL round trip price
        duration: flight.total_duration,
        stops: (flight.layovers?.length || 0),
        layovers: flight.layovers || [],
        departure_time: firstLeg.departure_airport?.time,
        arrival_time: lastLeg.arrival_airport?.time,
        departure_airport: firstLeg.departure_airport?.id,
        arrival_airport: lastLeg.arrival_airport?.id,
        booking_token: flight.booking_token, // Final booking token
        is_round_trip: true,
        aircraft: firstLeg.airplane,
      };
    });

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
