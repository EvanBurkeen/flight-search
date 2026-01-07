import { NextResponse } from 'next/server';

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const token = searchParams.get('token');
  const departureId = searchParams.get('departure_id');
  const arrivalId = searchParams.get('arrival_id');
  const outboundDate = searchParams.get('outbound_date');
  const apiKey = process.env.SERP_API_KEY;

  if (!token || !departureId || !arrivalId || !outboundDate) {
    return NextResponse.json({ error: 'Missing required parameters' }, { status: 400 });
  }

  try {
    // 1. Safe Parameter Encoding
    const params = new URLSearchParams({
      engine: "google_flights",
      booking_token: token || '',
      departure_id: departureId || '',
      arrival_id: arrivalId || '',
      outbound_date: outboundDate || '',
      type: "2", // Force one-way mode to avoid "return_date required" error
      gl: "us",  // Localization to prevent date/timezone errors
      hl: "en",
      currency: "USD",
      api_key: apiKey || ''
    });

    const response = await fetch(`https://serpapi.com/search.json?${params.toString()}`);
    const data = await response.json();

    if (data.error) {
      console.error("SerpApi Error:", data.error);
      return NextResponse.json({ error: data.error }, { status: 500 });
    }

    // 2. Primary Strategy: Direct Airline Link
    const airlineLink = data.booking_options?.[0]?.link;
    if (airlineLink) {
      return NextResponse.json({ url: airlineLink });
    } 
    
    // 3. Fallback Strategy: Google Flights Search Page
    const googleFlightsUrl = data.search_metadata?.google_flights_url;
    if (googleFlightsUrl) {
      console.log("No direct booking link; using fallback.");
      return NextResponse.json({ url: googleFlightsUrl });
    }

    return NextResponse.json({ error: 'No booking link found' }, { status: 404 });

  } catch (err) {
    console.error("Booking route error:", err);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
