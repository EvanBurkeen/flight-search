# Smart Flight Search - Web Interface

AI-powered flight search optimized for Delta Gold Medallion status. Beautiful chat interface with natural language understanding.

## Features

- 🤖 **AI-Powered**: Understands complex natural language queries
- ✈️ **Smart Ranking**: Prioritizes flights based on your Delta Gold status
- 💬 **Chat Interface**: Beautiful, responsive chat UI
- 🎯 **Multi-Criteria**: Handles refundability, legroom, time preferences, and more
- 🔄 **Round Trip Support**: Search outbound and return flights together
- 🚫 **Airline Filtering**: Exclude Spirit, Frontier, or any airline
- ⚡ **Real-Time**: Searches live Google Flights data via SerpAPI

## Tech Stack

- **Frontend**: Next.js 14 + React + TypeScript + Tailwind CSS
- **Backend**: Next.js API Routes
- **AI**: Claude (Anthropic) for natural language understanding
- **Flight Data**: SerpAPI (Google Flights scraper)
- **Deployment**: Vercel (1-click deploy from GitHub)

## Quick Start

### 1. Clone to GitHub

```bash
# Create a new repo on GitHub called "flight-search"
# Then on your Mac:

cd ~/Downloads
# Copy the flight-search-web folder to a new location
cp -r flight-search-web flight-search
cd flight-search

# Initialize git
git init
git add .
git commit -m "Initial commit: Smart Flight Search"

# Connect to GitHub
git remote add origin https://github.com/YOUR_USERNAME/flight-search.git
git branch -M main
git push -u origin main
```

### 2. Deploy to Vercel

1. Go to **https://vercel.com**
2. Sign in with GitHub
3. Click **"Add New Project"**
4. Import your `flight-search` repository
5. Configure:
   - **Framework Preset**: Next.js
   - **Root Directory**: `./` (keep default)
   - **Build Command**: `npm run build` (auto-detected)
   - **Output Directory**: `.next` (auto-detected)

6. **Add Environment Variables**:
   ```
   SERP_API_KEY=3664b05c846a43d9781951e186c243ff7309bd9e0fabd868338d2b11ef0d19e4
   ANTHROPIC_API_KEY=your_anthropic_key_here
   ```

7. Click **"Deploy"**

Vercel will:
- Install dependencies
- Build your app
- Deploy it to a URL like `flight-search-abc123.vercel.app`

Takes ~2 minutes!

### 3. Connect Your Custom Domain

Once deployed, in Vercel:

1. Go to your project → **Settings** → **Domains**
2. Add domain: `flights.evanburkeen.com` (subdomain recommended)
3. Vercel will show you DNS records to add

Then in your domain registrar (wherever you bought evanburkeen.com):

**Option A: Subdomain (Recommended)**
```
Type: CNAME
Name: flights
Value: cname.vercel-dns.com
```

**Option B: Main Domain Path**
Deploy to main domain, then use Next.js routing at `/flights`

### 4. Test It!

Visit your deployed URL and try:

```
I want a round trip from JFK to San Juan from 2/5 to 2/8. 
No Spirit or Frontier. Prefer direct flights.
```

## Local Development

Want to test locally first?

```bash
cd flight-search

# Install dependencies
npm install

# Create .env.local file
cp .env.example .env.local
# Edit .env.local and add your API keys

# Run development server
npm run dev

# Open http://localhost:3000
```

## Project Structure

```
flight-search/
├── app/
│   ├── api/
│   │   └── search/
│   │       └── route.ts          # API endpoint (handles searches)
│   ├── page.tsx                  # Main chat interface
│   ├── layout.tsx                # Root layout
│   └── globals.css               # Global styles
├── package.json                  # Dependencies
├── tsconfig.json                 # TypeScript config
├── tailwind.config.ts            # Tailwind CSS config
├── next.config.js                # Next.js config
└── README.md                     # This file
```

## How It Works

### 1. User Types Query
```
"JFK to London on 3/21, economy but show me first class if it's close in price"
```

### 2. Frontend → API Route
React component sends POST request to `/api/search`

### 3. Claude Parses Query
API route uses Anthropic Claude to understand the query and extract:
- Origin: JFK
- Destination: LHR
- Date: 2026-03-21
- Primary cabin: economy
- Compare cabins: [first]
- Max price difference: 300

### 4. Search Google Flights
API calls SerpAPI to scrape Google Flights with those parameters

### 5. Rank Results
Scores each flight based on:
- **Alliance** (30 pts): SkyTeam gets full points (Delta Gold benefits)
- **Direct flights** (15 pts): Time savings
- **Legroom** (10 pts): Wide-body aircraft preferred
- **Price** (10 pts): Relative to other options

### 6. Return to User
Beautiful cards showing:
- Airline & alliance
- Price & cabin class
- Departure/arrival times
- Score with progress bar
- Highlights (Delta Gold benefits, etc.)

## Example Queries

**Basic:**
```
JFK to London on 3/21
```

**Advanced:**
```
I need a refundable SkyTeam flight from New York to London on 3/21 
that leaves after 4pm. Show me economy but also check first class if 
the price difference is less than $300.
```

**Round Trip:**
```
Round trip JFK to San Juan 2/5 to 2/8, no Spirit or Frontier, 
prefer direct flights, needs to have carry-on included.
```

**Multiple Criteria:**
```
Find me a Delta or KLM business class flight from JFK to Amsterdam 
that's direct and refundable. I'm tall so need good legroom.
```

## Customization

### Change Your Loyalty Status

Edit `app/api/search/route.ts`:

```typescript
// Change scoring weights
if (alliance === 'SkyTeam') {
  score += 30;  // Increase this for more SkyTeam preference
  highlights.push('✓ SkyTeam - Full Delta Gold benefits');
}
```

### Adjust UI Colors

Edit `app/page.tsx`:

```typescript
// Change primary color from blue to your brand color
className="bg-blue-600 text-white"  // Change blue-600 to purple-600, etc.
```

### Add More Airlines to Alliances

Edit alliance arrays in `app/api/search/route.ts`:

```typescript
const SKYTEAM = ['DL', 'VS', 'AF', 'KL', ...]; // Add more codes
```

## Cost Breakdown

**Per Search:**
- SerpAPI: ~$0.02
- Anthropic Claude: ~$0.01
- Vercel hosting: Free (generous free tier)
- **Total: ~$0.03 per search**

**Monthly (100 searches):**
- SerpAPI: $2
- Anthropic: $1
- Vercel: $0
- **Total: ~$3/month**

Very affordable for personal use!

## Troubleshooting

### "API Key is invalid"
- Check environment variables are set correctly in Vercel
- Make sure no extra spaces in the keys
- Regenerate keys if needed

### "No flights found"
- Check date format (should be 2026, not 2025)
- Try simpler queries first
- Check SerpAPI dashboard for errors

### Build fails on Vercel
- Check all TypeScript errors locally first: `npm run build`
- Make sure all dependencies are in package.json
- Check Next.js version compatibility

### Slow response times
- Searches take 5-10 seconds (normal)
- LLM parsing + flight search + ranking = multiple API calls
- Can add loading indicators or optimize later

## Deployment Options

### Option 1: Subdomain (Recommended)
`flights.evanburkeen.com`
- Cleanest approach
- Easy DNS setup
- Dedicated experience

### Option 2: Path on Main Site
`evanburkeen.com/flights`
- Requires integrating with existing site
- More complex if you have another framework
- Good if you want everything under one domain

### Option 3: Separate Domain
`smartflights.com` or similar
- Could become its own product
- Easier to brand separately
- More flexibility

## Next Steps

**Immediate (Post-Deployment):**
1. Test with 10 different queries
2. Compare results to Google Flights
3. Verify Delta Gold ranking is working
4. Check mobile responsiveness

**Short Term (This Week):**
1. Add price tracking
2. Save favorite searches
3. Email alerts for price drops
4. Share results functionality

**Long Term (This Month):**
1. User accounts
2. Custom loyalty status
3. Historical price charts
4. Multi-city trips
5. Group bookings

## Support

**Issues?**
- Check Vercel deployment logs
- Test API endpoints directly: `https://your-url.vercel.app/api/search`
- Review SerpAPI dashboard
- Check Anthropic API usage

**Questions?**
Built by Evan Burkeen for personal use. Feel free to fork and customize!

## License

Personal use only. Not for commercial redistribution.

---

**Ready to deploy?** Push to GitHub, import to Vercel, add your API keys, and you're live! 🚀
