'use client';

import { useState, useRef, useEffect } from 'react';
import axios from 'axios';

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

type ViewMode = 'chat' | 'outbound' | 'return';

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'system',
      content: "I'm your intelligent flight search assistant.\n\nTry:\n• \"SFO to HND round trip 2/5-2/10\"\n• \"LAX to JFK one way next Friday\""
    }
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isBooking, setIsBooking] = useState(false);
  const [conversationHistory, setConversationHistory] = useState<any[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Two-step selection state
  const [viewMode, setViewMode] = useState<ViewMode>('chat');
  const [outboundFlights, setOutboundFlights] = useState<any[]>([]);
  const [selectedOutbound, setSelectedOutbound] = useState<any>(null);
  const [returnFlights, setReturnFlights] = useState<any[]>([]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const formatDuration = (minutes: number): string => {
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    return `${hours}h ${mins}m`;
  };

  const formatTime = (dateTimeString: string): string => {
    if (!dateTimeString) return '';
    const date = new Date(dateTimeString);
    return date.toLocaleTimeString('en-US', { 
      hour: 'numeric', 
      minute: '2-digit',
      hour12: true 
    });
  };

  const formatDate = (dateTimeString: string): string => {
    if (!dateTimeString) return '';
    const date = new Date(dateTimeString);
    return date.toLocaleDateString('en-US', { 
      month: 'short', 
      day: 'numeric'
    });
  };

  const formatPrice = (price: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 0
    }).format(price);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage = input.trim();
    setInput('');

    const newUserMessage: Message = { role: 'user', content: userMessage };
    setMessages(prev => [...prev, newUserMessage]);
    setConversationHistory(prev => [...prev, newUserMessage]);
    setIsLoading(true);

    // Reset on new search
    setViewMode('chat');
    setOutboundFlights([]);
    setSelectedOutbound(null);
    setReturnFlights([]);

    try {
      const response = await axios.post('/api/search', {
        query: userMessage,
        searchType: 'outbound',
        conversationHistory: conversationHistory
      });

      const { mode, message: responseMsg, results } = response.data;

      if (mode === 'clarify') {
        setMessages(prev => [...prev, { role: 'assistant', content: responseMsg }]);
        setConversationHistory(prev => [...prev, { role: 'assistant', content: responseMsg }]);
      } 
      else if (mode === 'search') {
        setMessages(prev => [...prev, { role: 'assistant', content: responseMsg }]);
        setConversationHistory(prev => [...prev, { role: 'assistant', content: responseMsg }]);

        const hasRoundTrips = results.some((f: any) => f.is_round_trip);
        if (hasRoundTrips) {
          setOutboundFlights(results);
          setViewMode('outbound');
        }
      }
      else if (mode === 'error') {
        setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${response.data.message}` }]);
      }
    } catch (error: any) {
      setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${error.message}` }]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleSelectOutbound = async (flight: any) => {
    if (!flight.booking_token) {
      alert('Missing departure token');
      return;
    }

    if (!flight.departure_id || !flight.arrival_id || !flight.outbound_date || !flight.return_date) {
      alert('Missing flight information');
      console.error('Flight data:', flight);
      return;
    }

    setSelectedOutbound(flight);
    setIsLoading(true);

    try {
      const response = await axios.post('/api/return-flights', {
        departure_token: flight.booking_token,
        departure_id: flight.departure_id,
        arrival_id: flight.arrival_id,
        outbound_date: flight.outbound_date,
        return_date: flight.return_date
      });

      setReturnFlights(response.data.results || []);
      setViewMode('return');
      setMessages(prev => [...prev, { 
        role: 'assistant', 
        content: response.data.message || 'Select your return flight:' 
      }]);
    } catch (error: any) {
      console.error('Return flights error:', error);
      alert('Failed to load return flights: ' + (error.response?.data?.error || error.message));
      setViewMode('chat'); // Go back to chat on error
    } finally {
      setIsLoading(false);
    }
  };

  const handleBookFlight = async (bookingToken: string) => {
    if (!bookingToken) {
      alert("Missing booking token");
      console.error('No booking token provided');
      return;
    }

    console.log('Booking with token:', bookingToken.substring(0, 30) + '...');
    setIsBooking(true);

    try {
      const response = await axios.get(`/api/booking?token=${encodeURIComponent(bookingToken)}`);
      
      if (response.data.url) {
        console.log('Opening booking URL:', response.data.url);
        window.open(response.data.url, '_blank');
      } else {
        console.error('No URL in response:', response.data);
        alert("Booking link unavailable. Please try another flight.");
      }
    } catch (error: any) {
      console.error("Booking Error:", error);
      const errorMsg = error.response?.data?.error || error.message || 'Unknown error';
      alert(`Booking failed: ${errorMsg}`);
    } finally {
      setIsBooking(false);
    }
  };

  const renderFlight = (flight: any, buttonText: string, onButtonClick: () => void) => (
    <div className="border border-black p-4 hover:opacity-90 transition-opacity mb-2" style={{ borderColor: 'rgba(0,0,0,0.12)' }}>
      <div className="flex justify-between items-start mb-2">
        <div>
          <div className="flex items-center gap-2">
            <div className="text-sm font-medium">{flight.airline}</div>
            {flight.is_round_trip && (
              <span className="text-xs px-2 py-0.5 bg-black text-white">↔ ROUND TRIP</span>
            )}
          </div>
        </div>
        <div className="text-xl font-normal">{formatPrice(flight.price)}</div>
      </div>

      <div className="text-xs font-medium mb-1" style={{ opacity: 0.85 }}>
        {flight.departure_airport} → {flight.layovers && flight.layovers.length > 0 
          ? flight.layovers.map((l: any) => l.id).join(' → ') + ' → ' 
          : ''}{flight.arrival_airport}
      </div>

      <div className="text-xs mb-3" style={{ opacity: 0.78 }}>
        {formatTime(flight.departure_time)} → {formatTime(flight.arrival_time)} · {formatDuration(flight.duration)} · {flight.stops === 0 ? 'Direct' : `${flight.stops} stop(s)`}
        {flight.layovers && flight.layovers.length > 0 && (
          <span className="block mt-1" style={{ opacity: 0.60 }}>
            via {flight.layovers.map((l: any) => `${l.name} (${formatDuration(l.duration)})`).join(', ')}
          </span>
        )}
      </div>

      <button
        onClick={onButtonClick}
        disabled={isBooking || isLoading}
        className="block w-full text-center text-xs py-2 border border-black hover:bg-black hover:text-white transition-all disabled:opacity-50"
      >
        {buttonText}
      </button>
    </div>
  );

  return (
    <div className="min-h-screen" style={{ backgroundColor: '#f7eed2' }}>
      <div className="container mx-auto px-6 py-12 max-w-4xl">
        <div className="mb-12">
          <h1 className="text-4xl font-bold text-black mb-1">Flight Search</h1>
          <p className="text-sm text-black" style={{ opacity: 0.72 }}>AI-powered with smart round trips</p>
        </div>

        {/* Chat Messages */}
        <div className="space-y-6 mb-8">
          {messages.map((msg, idx) => (
            <div key={idx}>
              {msg.role === 'system' && (
                <div className="text-sm leading-relaxed whitespace-pre-line border-l-2 border-black pl-4" style={{ opacity: 0.78 }}>
                  {msg.content}
                </div>
              )}
              {msg.role === 'user' && (
                <div className="text-right">
                  <div className="inline-block text-sm">→ {msg.content}</div>
                </div>
              )}
              {msg.role === 'assistant' && (
                <div className="text-sm leading-relaxed" style={{ opacity: 0.90 }}>
                  {msg.content}
                </div>
              )}
            </div>
          ))}
          {isLoading && <div className="text-sm" style={{ opacity: 0.60 }}>Searching...</div>}
          <div ref={messagesEndRef} />
        </div>

        {/* Outbound Flights */}
        {viewMode === 'outbound' && outboundFlights.length > 0 && (
          <div className="mb-8">
            <h2 className="text-xl font-bold mb-4">Select Outbound Flight</h2>
            {outboundFlights.map((flight, idx) => 
              renderFlight(flight, 'Select →', () => handleSelectOutbound(flight))
            )}
          </div>
        )}

        {/* Return Flights */}
        {viewMode === 'return' && (
          <div className="mb-8">
            <h2 className="text-xl font-bold mb-4">Select Return Flight</h2>
            <p className="text-sm mb-4" style={{ opacity: 0.72 }}>
              Outbound: {selectedOutbound?.departure_airport} → {selectedOutbound?.arrival_airport} on {formatDate(selectedOutbound?.departure_time)}
            </p>
            {returnFlights.length > 0 ? (
              returnFlights.map((flight, idx) => 
                renderFlight(flight, `Book Round Trip · ${formatPrice(flight.price)}`, () => handleBookFlight(flight.booking_token))
              )
            ) : (
              <div className="text-sm" style={{ opacity: 0.72 }}>
                No return flights available. Please try a different outbound flight.
              </div>
            )}
          </div>
        )}

        {/* Input */}
        <form onSubmit={handleSubmit} className="border-t border-black pt-6" style={{ borderColor: 'rgba(0,0,0,0.12)' }}>
          <div className="flex gap-3">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Describe your flight..."
              className="flex-1 text-sm border-b border-black outline-none py-2 bg-transparent"
              style={{ borderColor: 'rgba(0,0,0,0.25)' }}
              disabled={isLoading}
            />
            <button
              type="submit"
              disabled={isLoading || !input.trim()}
              className="text-xs px-6 py-2 border border-black hover:bg-black hover:text-white disabled:opacity-30 transition-all"
            >
              Search
            </button>
          </div>
        </form>

        <div className="text-center mt-16 text-xs" style={{ opacity: 0.60 }}>
          <a href="https://evanburkeen.com" className="hover:opacity-72 transition-opacity" style={{ textDecoration: 'underline' }}>
            Evan Burkeen
          </a>
        </div>
      </div>
    </div>
  );
}
