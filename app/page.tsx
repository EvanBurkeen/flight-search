'use client';

import { useState, useRef, useEffect } from 'react';
import axios from 'axios';

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
  results?: any;
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'system',
      content: "I'm your flight assistant. Natural language works—just describe what you need.\n\nExamples:\n• \"JFK to London March 21st, direct if possible\"\n• \"Miami round trip Feb 5-8, no budget airlines\"\n• \"San Francisco next week, I have Delta status\""
    }
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage = input.trim();
    setInput('');
    
    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    setIsLoading(true);

    try {
      const response = await axios.post('/api/search', {
        query: userMessage
      });

      setMessages(prev => [...prev, {
        role: 'assistant',
        content: response.data.message,
        results: response.data.results
      }]);
    } catch (error: any) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `Error: ${error.response?.data?.error || error.message}`
      }]);
    } finally {
      setIsLoading(false);
    }
  };

  const formatPrice = (price: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 0,
      maximumFractionDigits: 0
    }).format(price);
  };

  return (
    <div className="min-h-screen" style={{ backgroundColor: '#f7eed2' }}>
      <div className="container mx-auto px-6 py-12 max-w-4xl">
        {/* Header */}
        <div className="mb-12">
          <h1 className="text-4xl font-bold text-black mb-1" style={{ letterSpacing: '-0.03em' }}>
            Flight Search
          </h1>
          <p className="text-sm text-black" style={{ opacity: 0.72 }}>
            AI-powered
          </p>
        </div>

        {/* Chat Container */}
        <div className="space-y-6 mb-8">
          {messages.map((message, index) => (
            <div key={index} className="space-y-4">
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
                  
                  {/* Flight Results */}
                  {message.results && message.results.length > 0 && (
                    <div className="space-y-2 mt-6">
                      {message.results.slice(0, 5).map((flight: any, idx: number) => (
                        <div
                          key={idx}
                          className="border border-black p-4 hover:opacity-90 transition-opacity"
                          style={{ borderColor: 'rgba(0,0,0,0.12)' }}
                        >
                          {/* Header Row */}
                          <div className="flex justify-between items-start mb-2">
                            <div className="space-y-0.5">
                              <div className="text-sm font-medium">
                                {flight.airline}
                              </div>
                              <div className="text-xs" style={{ opacity: 0.60 }}>
                                {flight.airline_code} · {flight.alliance}
                              </div>
                            </div>
                            <div className="text-right">
                              <div className="text-xl font-normal">
                                {formatPrice(flight.price)}
                              </div>
                              <div className="text-xs" style={{ opacity: 0.60 }}>
                                {flight.cabin_class.replace('_', ' ')}
                              </div>
                            </div>
                          </div>
                          
                          {/* Flight Details */}
                          <div className="text-xs mb-2 space-y-0.5" style={{ opacity: 0.78 }}>
                            <div>
                              {flight.departure_time?.split('T')[1]?.slice(0, 5)} → {flight.arrival_time?.split('T')[1]?.slice(0, 5)}
                              {' · '}
                              {flight.duration ? `${Math.floor(flight.duration / 60)}h ${flight.duration % 60}m` : 'N/A'}
                              {' · '}
                              {flight.stops === 0 ? 'Direct' : `${flight.stops} stop${flight.stops > 1 ? 's' : ''}`}
                            </div>
                            <div style={{ opacity: 0.72 }}>
                              {flight.aircraft}
                            </div>
                          </div>

                          {/* Score Bar */}
                          <div className="mb-2">
                            <div className="h-px bg-black" style={{ opacity: 0.12 }}>
                              <div
                                className="h-px bg-black transition-all duration-500"
                                style={{ width: `${flight.score}%` }}
                              />
                            </div>
                          </div>

                          {/* Highlights */}
                          {flight.highlights && flight.highlights.length > 0 && (
                            <div className="mb-2 space-y-0.5">
                              {flight.highlights.map((h: string, i: number) => (
                                <div key={i} className="text-xs" style={{ opacity: 0.72 }}>
                                  {h}
                                </div>
                              ))}
                            </div>
                          )}

                          {/* Flight Details */}
                          {flight.details && (
                            <div className="mb-2 space-y-0.5 border-t pt-2" style={{ borderColor: 'rgba(0,0,0,0.08)' }}>
                              {flight.details.refundable && (
                                <div className="text-xs" style={{ opacity: 0.72 }}>
                                  {flight.details.refundable}
                                </div>
                              )}
                              {flight.details.carry_on && (
                                <div className="text-xs" style={{ opacity: 0.72 }}>
                                  Carry-on: {flight.details.carry_on}
                                </div>
                              )}
                              {flight.details.checked_bags && (
                                <div className="text-xs" style={{ opacity: 0.72 }}>
                                  Checked bag: {flight.details.checked_bags}
                                </div>
                              )}
                              {flight.details.change_fee && (
                                <div className="text-xs" style={{ opacity: 0.72 }}>
                                  {flight.details.change_fee}
                                </div>
                              )}
                              {flight.details.seat_selection && (
                                <div className="text-xs" style={{ opacity: 0.72 }}>
                                  Seat selection: {flight.details.seat_selection}
                                </div>
                              )}
                              {flight.raw_extensions && flight.raw_extensions.length > 0 && (
                                flight.raw_extensions.map((ext: string, i: number) => (
                                  <div key={i} className="text-xs" style={{ opacity: 0.60 }}>
                                    {ext}
                                  </div>
                                ))
                              )}
                            </div>
                          )}

                          {/* Book Button */}
                          {flight.booking_url && (
                            <a
                              href={flight.booking_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="block text-center text-xs py-2 border border-black hover:bg-black hover:text-white transition-all"
                              style={{ 
                                letterSpacing: '0.08em',
                                textTransform: 'uppercase'
                              }}
                            >
                              Book Flight
                            </a>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
          
          {isLoading && (
            <div className="text-sm" style={{ opacity: 0.60 }}>
              Searching...
            </div>
          )}
          
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <form onSubmit={handleSubmit} className="border-t border-black pt-6" style={{ borderColor: 'rgba(0,0,0,0.12)' }}>
          <div className="flex gap-3">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Describe your flight..."
              className="flex-1 text-sm border-b border-black outline-none py-2 bg-transparent"
              style={{ 
                borderColor: 'rgba(0,0,0,0.25)',
                backgroundColor: 'transparent'
              }}
              disabled={isLoading}
            />
            <button
              type="submit"
              disabled={isLoading || !input.trim()}
              className="text-xs px-6 py-2 border border-black hover:bg-black hover:text-white disabled:opacity-30 disabled:hover:bg-transparent transition-all"
              style={{ 
                letterSpacing: '0.08em',
                textTransform: 'uppercase',
                color: isLoading || !input.trim() ? 'rgba(0,0,0,0.30)' : '#000'
              }}
            >
              Search
            </button>
          </div>
          <div className="mt-4 text-xs" style={{ opacity: 0.60 }}>
            Powered by Google Flights + Claude AI
          </div>
        </form>

        {/* Footer */}
        <div className="text-center mt-16 text-xs" style={{ opacity: 0.60 }}>
          <a 
            href="https://evanburkeen.com" 
            className="hover:opacity-72 transition-opacity"
            style={{ 
              textDecoration: 'underline',
              textDecorationThickness: '1px',
              textUnderlineOffset: '2px'
            }}
          >
            Evan Burkeen
          </a>
        </div>
      </div>
    </div>
  );
}
