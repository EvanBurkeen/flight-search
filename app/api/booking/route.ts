import { NextResponse } from 'next/server';

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const token = searchParams.get('token');
  const departureId = searchParams.get('departure_id');
  const arrivalId = searchParams.get('arrival_id');
  const outboundDate = searchParams.get('outbound_date');
  const returnDate = searchParams.get('return_date');
  const apiKey = process.env.SERP_API_KEY;

  if (!token) {
    console.error('Booking: Missing token');
    return NextResponse.json({ error: 'booking_token is required' }, { status: 400 });
  }

  try {
    console.log('📝 Booking with token:', token.substring(0, 30) + '...');
    console.log('📍 Route:', departureId, '→', arrivalId, outboundDate, returnDate);

    // Build params - include route context if provided
    const params: Record<string, string> = {
      engine: "google_flights",
      booking_token: token,
      api_key: apiKey || '',
      currency: "USD",
      hl: "en",
      gl: "us"
    };

    // Add route context if available (required for round trips)
    if (departureId) params.departure_id = departureId;
    if (arrivalId) params.arrival_id = arrivalId;
    if (outboundDate) params.outbound_date = outboundDate;
    if (returnDate) params.return_date = returnDate;

    const urlParams = new URLSearchParams(params);
    const url = `https://serpapi.com/search.json?${urlParams.toString()}`;
    const response = await fetch(url);
    
    // Get response text first to log it if there's an error
    const responseText = await response.text();
    
    if (!response.ok) {
      console.error('SerpAPI HTTP error:', response.status);
      console.error('Response body:', responseText.substring(0, 500));
      
      // Parse error to see if we can extract useful info
      try {
        const errorData = JSON.parse(responseText);
        if (errorData.error) {
          console.error('SerpAPI error message:', errorData.error);
        }
      } catch (e) {
        // Response wasn't JSON
      }
      
      // For 400 errors, the booking token is likely invalid/expired
      // Return a generic Google Flights URL as fallback
      const fallbackUrl = 'https://www.google.com/travel/flights';
      console.log('⚠️ Using generic Google Flights fallback');
      return NextResponse.json({ 
        url: fallbackUrl,
        warning: 'Direct booking unavailable. Redirecting to Google Flights.'
      });
    }

    const data = JSON.parse(responseText);

    if (data.error) {
      console.error("SerpAPI Error:", data.error);
      // Still try to return a fallback URL
      const fallbackUrl = data.search_metadata?.google_flights_url || 'https://www.google.com/travel/flights';
      return NextResponse.json({ url: fallbackUrl });
    }

    // Try multiple possible fields for booking link
    const bookingOption = data.booking_options?.[0];
    const airlineLink = bookingOption?.link || 
                       bookingOption?.book_on_provider_link || 
                       bookingOption?.url;
    
    if (airlineLink) {
      console.log('✅ Found direct airline link');
      return NextResponse.json({ url: airlineLink });
    }
    
    // Log what we got to help debug
    if (data.booking_options && data.booking_options.length > 0) {
      console.log('Booking option keys:', Object.keys(data.booking_options[0]));
    }
    
    // Try Google Flights URL from search metadata
    const googleFlightsUrl = data.search_metadata?.google_flights_url;
    if (googleFlightsUrl) {
      console.log("⚠️ No direct booking link; using Google Flights");
      return NextResponse.json({ url: googleFlightsUrl });
    }

    // Last resort: generic Google Flights
    console.error('No booking URL found in response');
    return NextResponse.json({ 
      url: 'https://www.google.com/travel/flights',
      warning: 'No direct booking link available'
    });

  } catch (err: any) {
    console.error("Booking route error:", err);
    return NextResponse.json({ 
      url: 'https://www.google.com/travel/flights',
      error: 'Booking failed. Redirecting to Google Flights.'
    });
  }
}
