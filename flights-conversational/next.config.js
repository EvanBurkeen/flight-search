/** @type {import('next').NextConfig} */
const nextConfig = {
  env: {
    SERP_API_KEY: process.env.SERP_API_KEY,
    ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY,
  },
}

module.exports = nextConfig
