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
      content: "👋 Hi! I'm your smart flight assistant. I understand complex queries and factor in your Delta Gold Medallion status.\n\nTry asking:\n• \"JFK to London on 3/21, economy but show me first class if it's close\"\n• \"Round trip JFK to San Juan 2/5-2/8, no Spirit or Frontier\"\n• \"Find me a refundable SkyTeam flight after 4pm with good legroom\""
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
    
    // Add user message
    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    setIsLoading(true);

    try {
      const response = await axios.post('/api/search', {
        query: userMessage
      });

      // Add assistant response
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: response.data.message,
        results: response.data.results
      }]);
    } catch (error: any) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `❌ Error: ${error.response?.data?.error || error.message}`
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
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100">
      <div className="container mx-auto px-4 py-8 max-w-4xl">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-4xl font-bold text-gray-900 mb-2">
            ✈️ Smart Flight Search
          </h1>
          <p className="text-gray-600">
            Powered by AI • Optimized for your Delta Gold status
          </p>
        </div>

        {/* Chat Container */}
        <div className="bg-white rounded-2xl shadow-2xl overflow-hidden">
          {/* Messages */}
          <div className="h-[600px] overflow-y-auto p-6 space-y-4">
            {messages.map((message, index) => (
              <div
                key={index}
                className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`max-w-[80%] rounded-2xl px-6 py-4 ${
                    message.role === 'user'
                      ? 'bg-blue-600 text-white'
                      : message.role === 'system'
                      ? 'bg-gray-100 text-gray-700 border-2 border-gray-200'
                      : 'bg-gradient-to-r from-indigo-50 to-blue-50 text-gray-900 border border-indigo-200'
                  }`}
                >
                  <div className="whitespace-pre-wrap">{message.content}</div>
                  
                  {/* Flight Results */}
                  {message.results && message.results.length > 0 && (
                    <div className="mt-4 space-y-3">
                      {message.results.slice(0, 5).map((flight: any, idx: number) => (
                        <div
                          key={idx}
                          className="bg-white rounded-xl p-4 border-2 border-indigo-200 hover:border-indigo-400 transition-colors"
                        >
                          <div className="flex justify-between items-start mb-2">
                            <div>
                              <div className="font-bold text-lg text-gray-900">
                                {flight.airline} ({flight.airline_code})
                              </div>
                              <div className="text-sm text-gray-600">
                                {flight.alliance}
                              </div>
                            </div>
                            <div className="text-right">
                              <div className="text-2xl font-bold text-blue-600">
                                {formatPrice(flight.price)}
                              </div>
                              <div className="text-xs text-gray-500">
                                {flight.cabin_class}
                              </div>
                            </div>
                          </div>
                          
                          <div className="flex items-center text-sm text-gray-700 mb-2">
                            <span className="font-medium">
                              {flight.departure_time?.split('T')[1]?.slice(0, 5)} → {flight.arrival_time?.split('T')[1]?.slice(0, 5)}
                            </span>
                            <span className="mx-2">•</span>
                            <span>{flight.duration ? `${Math.floor(flight.duration / 60)}h ${flight.duration % 60}m` : 'N/A'}</span>
                            <span className="mx-2">•</span>
                            <span>{flight.stops === 0 ? 'Direct' : `${flight.stops} stop(s)`}</span>
                          </div>

                          <div className="text-xs text-gray-600 mb-2">
                            {flight.aircraft}
                          </div>

                          {/* Score */}
                          <div className="flex items-center justify-between">
                            <div className="flex-1 bg-gray-200 rounded-full h-2 mr-3">
                              <div
                                className="bg-gradient-to-r from-green-400 to-blue-500 h-2 rounded-full transition-all duration-500"
                                style={{ width: `${flight.score}%` }}
                              />
                            </div>
                            <div className="text-xs font-bold text-gray-700">
                              {Math.round(flight.score)}/100
                            </div>
                          </div>

                          {/* Highlights */}
                          {flight.highlights && flight.highlights.length > 0 && (
                            <div className="mt-2 space-y-1">
                              {flight.highlights.map((h: string, i: number) => (
                                <div key={i} className="text-xs text-green-700">
                                  {h}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
            
            {isLoading && (
              <div className="flex justify-start">
                <div className="bg-gray-100 rounded-2xl px-6 py-4">
                  <div className="flex items-center space-x-2">
                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                </div>
              </div>
            )}
            
            <div ref={messagesEndRef} />
          </div>

          {/* Input */}
          <form onSubmit={handleSubmit} className="border-t border-gray-200 p-4 bg-gray-50">
            <div className="flex space-x-3">
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Describe your flight needs..."
                className="flex-1 rounded-xl border-2 border-gray-300 px-4 py-3 focus:border-blue-500 focus:outline-none text-gray-900"
                disabled={isLoading}
              />
              <button
                type="submit"
                disabled={isLoading || !input.trim()}
                className="bg-blue-600 text-white px-8 py-3 rounded-xl font-semibold hover:bg-blue-700 disabled:bg-gray-400 disabled:cursor-not-allowed transition-colors"
              >
                {isLoading ? 'Searching...' : 'Search'}
              </button>
            </div>
            <div className="mt-2 text-xs text-gray-500 text-center">
              Powered by SerpAPI (Google Flights) + Claude AI
            </div>
          </form>
        </div>

        {/* Footer */}
        <div className="text-center mt-6 text-sm text-gray-600">
          <p>Built by Evan Burkeen • Optimized for Delta Gold Medallion Status</p>
        </div>
      </div>
    </div>
  );
}
