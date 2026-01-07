import { NextResponse } from 'next/server';

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const token = searchParams.get('token');
  const apiKey = process.env.SERP_API_KEY;

  if (!token) {
    return NextResponse.json({ error: 'booking_token is required' }, { status: 400 });
  }

  try {
    console.log('📝 Booking with token:', token.substring(0, 20) + '...');

    const params = new URLSearchParams({
      engine: "google_flights",
      booking_token: token,
      api_key: apiKey || ''
    });

    const response = await fetch(`https://serpapi.com/search.json?${params.toString()}`);
    const data = await response.json();

    if (data.error) {
      console.error("SerpApi Error:", data.error);
      return NextResponse.json({ error: data.error }, { status: 500 });
    }

    const airlineLink = data.booking_options?.[0]?.link;
    if (airlineLink) {
      console.log('✅ Redirecting to airline:', airlineLink);
      return NextResponse.json({ url: airlineLink });
    } 
    
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
