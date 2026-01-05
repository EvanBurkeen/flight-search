# Deploy to evanburkeen.com/flights

Three deployment strategies depending on your current setup.

---

## Strategy 1: Standalone Deployment with basePath (Recommended)

**Best if:** Your main site is not Next.js, or you want to keep projects separate.

### What This Does:
- Deploys flight search as its own Vercel project
- Configures it to work at `/flights` path
- Access at: `evanburkeen.com/flights`

### Steps:

1. **Deploy to Vercel**
```bash
cd flight-search-web
git init && git add . && git commit -m "Flight search"
git remote add origin https://github.com/YOUR_USERNAME/flight-search.git
git push -u origin main

# Then on Vercel.com:
# - Import repository
# - Add environment variables (SERP_API_KEY, ANTHROPIC_API_KEY)
# - Deploy
```

2. **Configure Domain in Vercel**

In Vercel project settings → Domains:
- Add: `evanburkeen.com`
- Point to this flight-search project

Vercel will give you DNS instructions like:
```
A Record: @ → 76.76.21.21
```

3. **The basePath is already configured**

`next.config.js` already has:
```javascript
basePath: '/flights'
```

This means:
- Homepage: `evanburkeen.com/flights`
- API: `evanburkeen.com/flights/api/search`
- Assets: `evanburkeen.com/flights/_next/...`

**✅ Done! Visit evanburkeen.com/flights**

---

## Strategy 2: Multi-Project with Rewrite (Keep Sites Separate)

**Best if:** You want your main site and flight search as separate projects.

### What This Does:
- Main site: `evanburkeen.com` (your current site)
- Flight search: Separate Vercel project
- Uses Vercel rewrite to proxy `/flights` requests

### Steps:

1. **Deploy flight search normally** (without custom domain)
```bash
# Deploy to Vercel
# You'll get: flight-search-abc123.vercel.app
```

2. **In your MAIN website's repo**, create/edit `vercel.json`:

```json
{
  "rewrites": [
    {
      "source": "/flights/:path*",
      "destination": "https://flight-search-abc123.vercel.app/:path*"
    }
  ]
}
```

3. **Remove basePath** from flight-search's `next.config.js`:

```javascript
/** @type {import('next').NextConfig} */
const nextConfig = {
  env: {
    SERP_API_KEY: process.env.SERP_API_KEY,
    ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY,
  },
  // NO basePath when using rewrite
}
```

4. **Push both changes**

```bash
# Main site:
git add vercel.json
git commit -m "Add flight search proxy"
git push

# Flight search:
# Update next.config.js, then push
```

**✅ Done! Visit evanburkeen.com/flights** (proxies to separate app)

---

## Strategy 3: Integrated (Add to Existing Next.js Site)

**Best if:** Your evanburkeen.com is already a Next.js site.

### Steps:

1. **In your existing site's repo:**

```bash
# Copy files from flight-search-web:
mkdir -p app/flights
cp ~/Downloads/flight-search-web/app/page.tsx app/flights/page.tsx
cp -r ~/Downloads/flight-search-web/app/api app/api

# Install dependencies
npm install @anthropic-ai/sdk axios
```

2. **Add environment variables** to `.env.local`:

```
SERP_API_KEY=3664b05c846a43d9781951e186c243ff7309bd9e0fabd868338d2b11ef0d19e4
ANTHROPIC_API_KEY=your_key
```

3. **Ensure Tailwind includes app/flights:**

`tailwind.config.ts`:
```typescript
content: [
  './app/**/*.{js,ts,jsx,tsx,mdx}',
  // This will include app/flights/page.tsx
],
```

4. **Push and deploy:**

```bash
git add .
git commit -m "Add flight search at /flights"
git push
```

**✅ Done! Visit evanburkeen.com/flights**

---

## Which Strategy Should You Use?

| Your Situation | Use Strategy | Why |
|----------------|--------------|-----|
| Main site is static HTML / WordPress / Hugo | **#1 (basePath)** | Simplest, everything in one domain |
| Want to keep projects separate | **#2 (Rewrite)** | Clean separation, independent deploys |
| Main site is already Next.js | **#3 (Integrated)** | Most efficient, shared dependencies |
| Not sure / easiest | **#1 (basePath)** | Just works, one project |

---

## Quick Comparison

### Strategy 1 (basePath) - RECOMMENDED
```
Pros:
✓ One project to manage
✓ Direct domain routing
✓ Simplest deployment
✓ Already configured

Cons:
✗ Main site must point to this project
  (or use a subdomain for main site)
```

### Strategy 2 (Rewrite)
```
Pros:
✓ Keep projects completely separate
✓ Independent deployments
✓ Main site stays unchanged

Cons:
✗ Requires vercel.json in main site
✗ Two projects to manage
✗ Slight proxy overhead
```

### Strategy 3 (Integrated)
```
Pros:
✓ Share dependencies
✓ Single build process
✓ Native Next.js routing

Cons:
✗ Only works if main site is Next.js
✗ Tightly coupled
```

---

## Testing Each Strategy

### Strategy 1:
```bash
# Local
npm run dev
# Visit: http://localhost:3000/flights

# Production
# Visit: https://evanburkeen.com/flights
```

### Strategy 2:
```bash
# Flight search project
npm run dev  # http://localhost:3000

# Main site handles /flights proxy in production
```

### Strategy 3:
```bash
# Your main site repo
npm run dev
# Visit: http://localhost:3000/flights
```

---

## What I Recommend for You

**Use Strategy 1 (basePath - already configured)**

Why:
1. ✅ It's **already set up** in the package I gave you
2. ✅ **One command deploy** - just push to Vercel
3. ✅ **Simple DNS** - point your domain and done
4. ✅ **No extra configuration** needed
5. ✅ **Works immediately** at /flights path

Steps:
```bash
cd flight-search-web
git init && git add . && git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/flight-search.git
git push -u origin main

# Vercel.com → Import → Add domain: evanburkeen.com
# Done!
```

---

## Current State of Package

The package I gave you is **already configured for Strategy 1**:

✅ `next.config.js` has `basePath: '/flights'`
✅ `vercel.json` has rewrite rules
✅ All paths will work at `/flights`

**Just deploy it!**

---

## Need to Switch Strategies?

**To use Strategy 2 instead:**
1. Remove `basePath: '/flights'` from `next.config.js`
2. Deploy without custom domain
3. Add rewrite to your main site's vercel.json

**To use Strategy 3 instead:**
1. Copy files into your existing Next.js repo
2. No config changes needed
3. Next.js automatically handles /flights route

---

**Recommendation: Go with what's already set up (Strategy 1).** Deploy, point your domain, and you're done! 🚀
