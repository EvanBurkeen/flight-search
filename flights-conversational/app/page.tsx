'use client';

import { useState, useRef, useEffect } from 'react';
import axios from 'axios';

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
  results?: any[];
  isOutbound?: boolean;
  isReturn?: boolean;
  selectedOutbound?: any;
  context?: any; // For clarification context
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'system',
      content: "I'm your intelligent flight search assistant.\n\nTry:\n• \"New York to Paris\" (I'll ask when)\n• \"LAX to SFO next Friday\"\n• \"Check the week after\" (remembers your last search)"
    }
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isBooking, setIsBooking] = useState(false);
  const [originalQuery, setOriginalQuery] = useState('');
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
    
    // Add user message to conversation
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

      const { mode, message: responseMsg, results, isRoundTrip, context, searchCriteria } = response.data;

      // CLARIFICATION MODE: Assistant asks for more info
      if (mode === 'clarify') {
        const clarifyMessage: Message = {
          role: 'assistant',
          content: responseMsg,
          context: context
        };
        setMessages(prev => [...prev, clarifyMessage]);
        setConversationHistory(prev => [...prev, clarifyMessage]);
        
        // Store partial context for next message
        if (searchCriteria) {
          setOriginalQuery(userMessage);
        }
      } 
      // SEARCH MODE: Display flight results
      else if (mode === 'search') {
        const searchMessage: Message = {
          role: 'assistant',
          content: responseMsg,
          results: results,
          isOutbound: isRoundTrip
        };
        setMessages(prev => [...prev, searchMessage]);
        setConversationHistory(prev => [...prev, { role: 'assistant', content: responseMsg }]);
        
        if (isRoundTrip) {
          setOriginalQuery(userMessage);
        }
      }
      // ERROR MODE
      else if (mode === 'error') {
        const errorMessage: Message = {
          role: 'assistant',
          content: `Error: ${response.data.error}`
        };
        setMessages(prev => [...prev, errorMessage]);
      }
    } catch (error: any) {
      const catchErrorMessage: Message = {
        role: 'assistant',
        content: `Error: ${error.response?.data?.error || error.message}`
      };
      setMessages(prev => [...prev, catchErrorMessage]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleSelectOutbound = async (flight: any) => {
    setIsLoading(true);
    const loadingMessage: Message = {
      role: 'assistant',
      content: 'Searching return flights...'
    };
    setMessages(prev => [...prev, loadingMessage]);

    try {
      const response = await axios.post('/api/search', {
        query: originalQuery,
        searchType: 'return',
        selectedOutbound: flight,
        conversationHistory: conversationHistory
      });

      const returnMessage: Message = {
        role: 'assistant',
        content: response.data.message,
        results: response.data.results,
        isReturn: true,
        selectedOutbound: flight
      };
      setMessages(prev => [...prev.slice(0, -1), returnMessage]);
    } catch (error: any) {
      const errorMessage: Message = {
        role: 'assistant',
        content: `Error: ${error.response?.data?.error || error.message}`
      };
      setMessages(prev => [...prev.slice(0, -1), errorMessage]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleBookFlight = async (token: string, departureId: string, arrivalId: string, outboundDate: string) => {
    if (!token || !departureId || !arrivalId || !outboundDate) {
      alert("Missing required flight data for booking.");
      return;
    }
    setIsBooking(true);
    try {
      const response = await axios.get(
        `/api/booking?token=${token}&departure_id=${departureId}&arrival_id=${arrivalId}&outbound_date=${outboundDate}`
      );
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
            AI-powered
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
                  
                  {message.isReturn && message.selectedOutbound && (
                    <div className="p-4 border border-black mb-4" style={{ borderColor: 'rgba(0,0,0,0.20)', backgroundColor: 'rgba(0,0,0,0.03)' }}>
                      <div className="text-xs mb-1" style={{ opacity: 0.60, textTransform: 'uppercase', letterSpacing: '0.08em' }}>
                        Selected Outbound
                      </div>
                      <div className="text-sm font-medium">
                        {message.selectedOutbound.airline} · {formatPrice(message.selectedOutbound.price)}
                      </div>
                      <div className="text-xs" style={{ opacity: 0.72 }}>
                        {message.selectedOutbound.departure_time && message.selectedOutbound.arrival_time ? (
                          <>
                            {message.selectedOutbound.departure_time} → {message.selectedOutbound.arrival_time}
                            {' · '}
                          </>
                        ) : null}
                        {message.selectedOutbound.stops === 0 ? 'Direct' : `${message.selectedOutbound.stops} stop(s)`}
                      </div>
                    </div>
                  )}
                  
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
                              <div className="text-sm font-medium">{flight.airline}</div>
                              <div className="text-xs" style={{ opacity: 0.60 }}>
                                {flight.airline_code}
                              </div>
                            </div>
                            <div className="text-xl font-normal">
                              {formatPrice(flight.price)}
                            </div>
                          </div>
                          
                          {/* Route with airport codes */}
                          <div className="text-xs font-medium mb-1" style={{ opacity: 0.85, letterSpacing: '0.05em' }}>
                            {flight.departure_airport} → {flight.arrival_airport}
                          </div>
                          
                          <div className="text-xs mb-2" style={{ opacity: 0.78 }}>
                            {flight.departure_time} → {flight.arrival_time} · {flight.duration ? `${Math.floor(flight.duration / 60)}h ${flight.duration % 60}m · ` : ''} {flight.stops === 0 ? 'Direct' : `${flight.stops} stop(s)`}
                          </div>

                          {flight.aircraft && (
                            <div className="text-xs mb-2" style={{ opacity: 0.60 }}>
                              {flight.aircraft}
                            </div>
                          )}

                          {message.isOutbound ? (
                            <button
                              onClick={() => handleSelectOutbound(flight)}
                              disabled={isLoading}
                              className="block w-full text-center text-xs py-2 border border-black hover:bg-black hover:text-white transition-all disabled:opacity-30"
                              style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}
                            >
                              Select Outbound
                            </button>
                          ) : (
                            <button
                              onClick={() => handleBookFlight(
                                flight.booking_token, 
                                flight.departure_id,
                                flight.arrival_id,
                                flight.outbound_date
                              )}
                              disabled={isBooking}
                              className="block w-full text-center text-xs py-2 border border-black hover:bg-black hover:text-white transition-all disabled:opacity-50"
                              style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}
                            >
                              {isBooking ? 'Redirecting...' : message.isReturn && message.selectedOutbound 
                                ? `Book Round Trip · ${formatPrice(message.selectedOutbound.price + flight.price)}` 
                                : `Book on ${flight.airline || 'Airline'}`}
                            </button>
                          )}
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
