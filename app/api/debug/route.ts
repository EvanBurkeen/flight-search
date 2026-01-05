import { NextRequest, NextResponse } from 'next/server';

export async function GET(request: NextRequest) {
  try {
    // Test SerpAPI
    const params = {
      engine: 'google_flights',
      departure_id: 'JFK',
      arrival_id: 'SJU',
      outbound_date: '2026-02-05',
      currency: 'USD',
      hl: 'en',
      adults: 1,
      type: '2',
      travel_class: '1',
      api_key: process.env.SERP_API_KEY!,
    };

    const url = `https://serpapi.com/search.json?${new URLSearchParams(params).toString()}`;
    
    const response = await fetch(url);
    const data = await response.json();

    const debugInfo = {
      serpapi_status: response.status,
      flights_found: data.best_flights?.length || 0,
      other_flights_found: data.other_flights?.length || 0,
      total_flights: (data.best_flights?.length || 0) + (data.other_flights?.length || 0),
      has_error: !!data.error,
      error_message: data.error || null,
      sample_flight: data.best_flights?.[0] || data.other_flights?.[0] || null,
      search_params: {
        origin: 'JFK',
        destination: 'SJU',
        date: '2026-02-05',
      }
    };

    return NextResponse.json(debugInfo, { status: 200 });
  } catch (error: any) {
    return NextResponse.json({
      error: error.message,
      stack: error.stack
    }, { status: 500 });
  }
}
