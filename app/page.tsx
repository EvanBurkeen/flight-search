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
    <div className="min-h-screen bg-white">
      <div className="container mx-auto px-6 py-12 max-w-4xl">
        {/* Header */}
        <div className="mb-16">
          <h1 className="text-3xl font-normal text-black mb-2 tracking-tight">
            Flight Search
          </h1>
          <p className="text-gray-500 text-sm font-light">
            AI-powered
          </p>
        </div>

        {/* Chat Container */}
        <div className="space-y-8 mb-8">
          {messages.map((message, index) => (
            <div key={index} className="space-y-4">
              {message.role === 'system' && (
                <div className="text-sm text-gray-600 leading-relaxed whitespace-pre-line font-light border-l-2 border-gray-200 pl-4">
                  {message.content}
                </div>
              )}
              
              {message.role === 'user' && (
                <div className="text-right">
                  <div className="inline-block text-sm text-gray-900 font-light">
                    → {message.content}
                  </div>
                </div>
              )}
              
              {message.role === 'assistant' && (
                <div className="space-y-4">
                  <div className="text-sm text-gray-700 leading-relaxed whitespace-pre-line font-light">
                    {message.content}
                  </div>
                  
                  {/* Flight Results */}
                  {message.results && message.results.length > 0 && (
                    <div className="space-y-3 mt-6">
                      {message.results.slice(0, 5).map((flight: any, idx: number) => (
                        <div
                          key={idx}
                          className="border border-gray-200 hover:border-gray-300 transition-colors p-5 group"
                        >
                          {/* Header Row */}
                          <div className="flex justify-between items-start mb-3">
                            <div className="space-y-0.5">
                              <div className="text-sm font-medium text-black">
                                {flight.airline}
                              </div>
                              <div className="text-xs text-gray-500 font-light">
                                {flight.airline_code} · {flight.alliance}
                              </div>
                            </div>
                            <div className="text-right">
                              <div className="text-xl font-light text-black">
                                {formatPrice(flight.price)}
                              </div>
                              <div className="text-xs text-gray-500 font-light">
                                {flight.cabin_class.replace('_', ' ')}
                              </div>
                            </div>
                          </div>
                          
                          {/* Flight Details */}
                          <div className="text-xs text-gray-600 mb-3 font-light space-y-1">
                            <div>
                              {flight.departure_time?.split('T')[1]?.slice(0, 5)} → {flight.arrival_time?.split('T')[1]?.slice(0, 5)}
                              {' · '}
                              {flight.duration ? `${Math.floor(flight.duration / 60)}h ${flight.duration % 60}m` : 'N/A'}
                              {' · '}
                              {flight.stops === 0 ? 'Direct' : `${flight.stops} stop${flight.stops > 1 ? 's' : ''}`}
                            </div>
                            <div className="text-gray-500">
                              {flight.aircraft}
                            </div>
                          </div>

                          {/* Score Bar */}
                          <div className="mb-3">
                            <div className="h-0.5 bg-gray-100">
                              <div
                                className="h-0.5 bg-black transition-all duration-500"
                                style={{ width: `${flight.score}%` }}
                              />
                            </div>
                          </div>

                          {/* Highlights */}
                          {flight.highlights && flight.highlights.length > 0 && (
                            <div className="mb-3 space-y-0.5">
                              {flight.highlights.map((h: string, i: number) => (
                                <div key={i} className="text-xs text-gray-600 font-light">
                                  {h}
                                </div>
                              ))}
                            </div>
                          )}

                          {/* Flight Details */}
                          {flight.details && (
                            <div className="mb-3 space-y-0.5 border-t border-gray-100 pt-2">
                              {flight.details.refundable && (
                                <div className="text-xs text-gray-600 font-light">
                                  {flight.details.refundable}
                                </div>
                              )}
                              {flight.details.carry_on && (
                                <div className="text-xs text-gray-600 font-light">
                                  Carry-on: {flight.details.carry_on}
                                </div>
                              )}
                              {flight.details.checked_bags && (
                                <div className="text-xs text-gray-600 font-light">
                                  Checked bag: {flight.details.checked_bags}
                                </div>
                              )}
                              {flight.details.change_fee && (
                                <div className="text-xs text-gray-600 font-light">
                                  {flight.details.change_fee}
                                </div>
                              )}
                              {flight.details.seat_selection && (
                                <div className="text-xs text-gray-600 font-light">
                                  Seat selection: {flight.details.seat_selection}
                                </div>
                              )}
                              {flight.raw_extensions && flight.raw_extensions.length > 0 && (
                                flight.raw_extensions.map((ext: string, i: number) => (
                                  <div key={i} className="text-xs text-gray-500 font-light">
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
                              className="block text-center text-xs py-2 border border-black text-black hover:bg-black hover:text-white transition-colors font-light tracking-wide"
                            >
                              BOOK FLIGHT
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
            <div className="text-sm text-gray-400 font-light">
              Searching...
            </div>
          )}
          
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <form onSubmit={handleSubmit} className="border-t border-gray-200 pt-6">
          <div className="flex gap-3">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Describe your flight..."
              className="flex-1 text-sm border-b border-gray-300 focus:border-black outline-none py-2 font-light placeholder-gray-400 bg-transparent"
              disabled={isLoading}
            />
            <button
              type="submit"
              disabled={isLoading || !input.trim()}
              className="text-sm px-6 py-2 border border-black text-black hover:bg-black hover:text-white disabled:border-gray-300 disabled:text-gray-300 disabled:hover:bg-transparent disabled:hover:text-gray-300 transition-colors font-light tracking-wide"
            >
              SEARCH
            </button>
          </div>
          <div className="mt-4 text-xs text-gray-400 font-light">
            Powered by Google Flights + Claude AI
          </div>
        </form>

        {/* Footer */}
        <div className="text-center mt-16 text-xs text-gray-400 font-light">
          <a href="https://evanburkeen.com" className="hover:text-gray-600 transition-colors">
            Evan Burkeen
          </a>
        </div>
      </div>
    </div>
  );
}
