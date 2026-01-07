# Conversational Flight Search - v2.0

AI-powered flight search with **true conversational intelligence**.

## What's New in v2.0

### 🧠 Intelligent Conversations
- **Multi-airport search**: "New York to Paris" → searches JFK, EWR, LGA → CDG, ORY
- **Context memory**: "Check the week after" remembers your last search
- **Progressive disclosure**: Asks clarifying questions when info is missing
- **Natural language**: Talk naturally, no need for exact formats

### ✅ Proven Production Features (from v1.0)
- **Working booking links**: Direct airline deep links via SerpAPI
- **Secure API handling**: Keys hidden on backend
- **Round trip support**: Two-step selection flow
- **Parameter encoding**: URLSearchParams for safe token handling
- **Comprehensive airline mapping**: Spirit, Frontier, JetBlue, etc.

---

## Quick Start

### 1. Install Dependencies
```bash
npm install
```

### 2. Set Environment Variables
Create `.env.local`:
```
SERP_API_KEY=your_serpapi_key
ANTHROPIC_API_KEY=your_anthropic_key
```

### 3. Run Development Server
```bash
npm run dev
```

Open http://localhost:3000

---

## Example Conversations

### Progressive Disclosure
```
You: "New York to Paris"
AI: "When would you like to fly? I can search from JFK, Newark, and LaGuardia to CDG and Orly."
You: "Next Friday"
AI: [Shows flights for Jan 10]
```

### Context Memory
```
You: "LAX to SFO on 2/5"
AI: [Shows flights]
You: "Check the week after"
AI: [Shows flights for 2/12 - remembers LAX→SFO!]
```

### Natural Language
```
You: "I want to go to Paris"
AI: "When would you like to travel? Where are you flying from?"
You: "From New York, next month"
AI: [Intelligently searches all NYC → Paris airports]
```

---

## Architecture

### Two-Mode System

**CLARIFICATION MODE**
- Missing info (date, origin, destination)
- Claude asks specific questions
- Stores partial context

**SEARCH MODE**
- All info present
- Executes flight search via SerpAPI
- Shows results

### API Flow

```
User Message + History
    ↓
POST /api/search
    ↓
Claude Sonnet 4
    ↓
Decide: clarify OR search
    ↓
If search → SerpAPI
    ↓
Return results
```

### Booking Flow

```
User clicks "Book"
    ↓
GET /api/booking
    ↓
SerpAPI booking token resolution
    ↓
Direct airline URL
    ↓
Opens in new tab
```

---

## Supported Cities (Multi-Airport)

| City | Airports |
|------|----------|
| New York | JFK, EWR, LGA |
| Paris | CDG, ORY |
| London | LHR, LGW, STN, LTN |
| San Francisco | SFO, OAK, SJC |
| Washington DC | DCA, IAD, BWI |
| Miami | MIA, FLL |
| Chicago | ORD, MDW |
| Dallas | DFW, DAL |
| Houston | IAH, HOU |

Easy to add more in `AIRPORT_MAPPINGS` dictionary.

---

## File Structure

```
app/
  ├── page.tsx                    # Frontend with conversation history
  ├── layout.tsx                  # Layout with Inter font
  ├── globals.css                 # Tailwind CSS
  └── api/
      ├── search/
      │   └── route.ts            # AI parsing + SerpAPI search
      └── booking/
          └── route.ts            # Booking token resolution
```

---

## Key Features

### Backend Intelligence
✅ City-to-airport mapping (automatic)
✅ Conversation history tracking
✅ Relative date parsing ("next week", "tomorrow")
✅ Airline name → code conversion
✅ Smart clarification questions
✅ Secure API key handling

### Frontend Polish
✅ Clean minimal design
✅ Real-time conversation flow
✅ Loading states
✅ Error handling
✅ Responsive layout
✅ Direct booking buttons

---

## Deployment

### Vercel

```bash
# Push to GitHub
git add .
git commit -m "Add conversational AI flight search"
git push origin main

# Deploy automatically on Vercel
# Add environment variables in Vercel dashboard
```

### Environment Variables (Vercel)
```
SERP_API_KEY=your_key
ANTHROPIC_API_KEY=your_key
```

---

## Testing Scenarios

After deploying, test:

1. **"New York to Paris"** → Should ask for date, mention all airports
2. **"LAX to SFO 2/5"** then **"week after"** → Second search auto-calculates 2/12
3. **"I want to fly to Miami"** → Should ask where from and when
4. **"JFK to LAX tomorrow"** then **"what about Sunday"** → Remembers route

---

## What Makes This Better

### vs Traditional Search
❌ Traditional: Dropdown menus, rigid forms
✅ This: Natural conversation, flexible input

### vs Simple LLM Parser
❌ Simple: One-shot parsing, no context
✅ This: Multi-turn conversation with memory

### vs Generic Chatbot
❌ Generic: Vague responses, no action
✅ This: Concrete results, direct booking

---

## Future Enhancements

Possible additions:
- Price comparison ("show me cheaper options")
- Flexible dates ("cheapest in March")
- Multi-city itineraries
- Saved preferences
- Price alerts

---

## Tech Stack

- **Next.js 14** - React framework
- **TypeScript** - Type safety
- **Tailwind CSS** - Styling
- **Claude Sonnet 4** - Conversational AI
- **SerpAPI** - Google Flights data
- **Vercel** - Hosting

---

## Credits

Built by Evan Burkeen
- Website: https://evanburkeen.com
- Flight Search: https://flights.evanburkeen.com

---

## License

MIT
