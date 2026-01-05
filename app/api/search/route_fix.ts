// This is the fixed evaluateFlight function - copy this into route.ts

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
      return null;
    }
    
    // ONLY filter alliance if user specified AND we know the alliance
    if (criteria.alliance_preference && 
        criteria.alliance_preference !== 'any' && 
        alliance !== 'Independent' &&
        alliance !== criteria.alliance_preference) {
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
          if (criteria.departure_time_before && depHour >= criteria.departure_time_before) return null;
          if (criteria.departure_time_after && depHour < criteria.departure_time_after) return null;
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
    if (criteria.must_be_direct && stops > 0) return null;

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
    } else if (extText.includes('non-refundable') && criteria.must_be_refundable) {
      return null;
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
