'use client';

import { useState, useRef, useEffect } from 'react';
import axios from 'axios';

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
  results?: any[];
  outbounds?: any[];
  returns?: any[];
  selectedOutbound?: any;
  context?: any;
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'system',
      content: "I'm your intelligent flight search assistant.\n\nTry:\n• \"JFK to Paris round trip 2/5-2/8\"\n• \"Cheapest from New York to Europe\"\n• \"LAX to SFO next Friday\""
    }
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isBooking, setIsBooking] = useState(false);
  const [conversationHistory, setConversationHistory] = useState<any[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage = input.trim();
    setInput('');

    const newUserMessage: Message = { role: 'user', content: userMessage };
    setMessages(prev => [...prev, newUserMessage]);
    setConversationHistory(prev => [...prev, newUserMessage]);
    setIsLoading(true);

    try {
      const response = await axios.post('/api/search', {
        query: userMessage,
        searchType: 'outbound',
        conversationHistory: conversationHistory
      });

      const { mode, message: responseMsg, results, context } = response.data;

      if (mode === 'clarify') {
        const clarifyMessage: Message = {
          role: 'assistant',
          content: responseMsg,
          context: context
        };
        setMessages(prev => [...prev, clarifyMessage]);
        setConversationHistory(prev => [...prev, clarifyMessage]);
      } 
      else if (mode === 'search') {
        const searchMessage: Message = {
          role: 'assistant',
          content: responseMsg,
          results: results
        };
        setMessages(prev => [...prev, searchMessage]);
        setConversationHistory(prev => [...prev, { role: 'assistant', content: responseMsg }]);
      }
      else if (mode === 'error') {
        const errorMessage: Message = {
          role: 'assistant',
          content: `Error: ${response.data.message}`
        };
        setMessages(prev => [...prev, errorMessage]);
      }
    } catch (error: any) {
      const errorMessage: Message = {
        role: 'assistant',
        content: `Error: ${error.response?.data?.message || error.message}`
      };
      setMessages(prev => [...prev, errorMessage]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleBookFlight = async (token: string, departureId: string, arrivalId: string, outboundDate: string, returnDate?: string) => {
    if (!token || !departureId || !arrivalId || !outboundDate) {
      alert("Missing required flight data for booking.");
      return;
    }
    setIsBooking(true);
    try {
      const params = new URLSearchParams({
        token,
        departure_id: departureId,
        arrival_id: arrivalId,
        outbound_date: outboundDate,
      });
      
      if (returnDate) {
        params.append('return_date', returnDate);
      }

      const response = await axios.get(`/api/booking?${params.toString()}`);
      
      if (response.data.url) {
        window.open(response.data.url, '_blank', 'noopener,noreferrer');
      } else {
        alert("Booking link unavailable. Please try another flight.");
      }
    } catch (error) {
      console.error("Booking Error:", error);
      alert("Error retrieving booking link.");
    } finally {
      setIsBooking(false);
    }
  };

  const formatPrice = (price: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 0
    }).format(price);
  };

  return (
    <div className="min-h-screen" style={{ backgroundColor: '#f7eed2' }}>
      <div className="container mx-auto px-6 py-12 max-w-4xl">
        <div className="mb-12">
          <h1 className="text-4xl font-bold text-black mb-1" style={{ letterSpacing: '-0.03em' }}>
            Flight Search
          </h1>
          <p className="text-sm text-black" style={{ opacity: 0.72 }}>
            AI-powered with smart round trips
          </p>
        </div>

        <div className="space-y-6 mb-8">
          {messages.map((message, index) => (
            <div key={index}>
              {message.role === 'system' && (
                <div className="text-sm leading-relaxed whitespace-pre-line border-l-2 border-black pl-4" style={{ opacity: 0.78 }}>
                  {message.content}
                </div>
              )}

              {message.role === 'user' && (
                <div className="text-right">
                  <div className="inline-block text-sm">
                    → {message.content}
                  </div>
                </div>
              )}

              {message.role === 'assistant' && (
                <div className="space-y-4">
                  <div className="text-sm leading-relaxed whitespace-pre-line" style={{ opacity: 0.90 }}>
                    {message.content}
                  </div>

                  {/* ONE-WAY AND ROUND-TRIP RESULTS */}
                  {message.results && message.results.length > 0 && (
                    <div className="space-y-2 mt-6">
                      {message.results.map((flight: any, idx: number) => (
                        <div
                          key={idx}
                          className="border border-black p-4 hover:opacity-90 transition-opacity"
                          style={{ borderColor: 'rgba(0,0,0,0.12)' }}
                        >
                          <div className="flex justify-between items-start mb-2">
                            <div>
                              <div className="flex items-center gap-2">
                                <div className="text-sm font-medium">{flight.airline}</div>
                                {flight.is_round_trip && (
                                  <span className="text-xs px-2 py-0.5 bg-black text-white" style={{ letterSpacing: '0.05em' }}>
                                    ↔ ROUND TRIP
                                  </span>
                                )}
                              </div>
                              <div className="text-xs" style={{ opacity: 0.60 }}>
                                {flight.airline_code}
                              </div>
                            </div>
                            <div className="text-xl font-normal">
                              {formatPrice(flight.price)}
                            </div>
                          </div>

                          <div className="text-xs font-medium mb-1" style={{ opacity: 0.85, letterSpacing: '0.05em' }}>
                            {flight.departure_airport} → {flight.arrival_airport}
                          </div>

                          <div className="text-xs mb-3" style={{ opacity: 0.78 }}>
                            {flight.departure_time} → {flight.arrival_time} · {flight.duration ? `${Math.floor(flight.duration / 60)}h ${flight.duration % 60}m · ` : ''} {flight.stops === 0 ? 'Direct' : `${flight.stops} stop(s)`}
                          </div>

                          <button
                            onClick={() => handleBookFlight(
                              flight.booking_token,
                              flight.departure_id,
                              flight.arrival_id,
                              flight.outbound_date,
                              flight.return_date
                            )}
                            disabled={isBooking}
                            className="block w-full text-center text-xs py-2 border border-black hover:bg-black hover:text-white transition-all disabled:opacity-50"
                            style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}
                          >
                            {isBooking ? 'Redirecting...' : `Book ${flight.is_round_trip ? 'Round Trip' : 'Flight'} · ${formatPrice(flight.price)}`}
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}

          {isLoading && <div className="text-sm" style={{ opacity: 0.60 }}>Searching...</div>}
          <div ref={messagesEndRef} />
        </div>

        <form onSubmit={handleSubmit} className="border-t border-black pt-6" style={{ borderColor: 'rgba(0,0,0,0.12)' }}>
          <div className="flex gap-3">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Describe your flight..."
              className="flex-1 text-sm border-b border-black outline-none py-2 bg-transparent"
              style={{ borderColor: 'rgba(0,0,0,0.25)', backgroundColor: 'transparent' }}
              disabled={isLoading}
            />
            <button
              type="submit"
              disabled={isLoading || !input.trim()}
              className="text-xs px-6 py-2 border border-black hover:bg-black hover:text-white disabled:opacity-30 transition-all"
              style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}
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
