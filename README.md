# StockFinder Swing Backend

Private backend for the StockFinder tablet PWA.

## What it does
- Groww API connector
- Live quote endpoint
- Daily token refresh endpoint
- Historical daily candles
- RSI, EMA20/50/200, MACD, ATR, volume ratio
- Basic breakout, bullish engulfing, double-bottom and ascending-triangle recognition
- 0–100 swing score
- `/api/scan/{symbol}` consumed by the tablet app

## Deploy without a laptop
This repository is ready for a cloud Python service such as Render. Upload the folder to a Git repository from your tablet, then create a web service from `render.yaml`.

Set secrets as environment variables:
GROWW_API_KEY
GROWW_API_SECRET

Do NOT commit either secret to GitHub.

For the current Groww API, API-key/secret authentication uses a checksum generated from `secret + current epoch timestamp` and requires daily approval on the Groww Cloud API Keys page. TOTP authentication is also supported. See the official Groww documentation.

## Connect the PWA
After deployment, copy the backend HTTPS URL and set it in the PWA browser localStorage under `stockfinder_backend`, or update the single-file HTML before hosting.

## Safety
This backend is for research/scanning. It deliberately contains no order-placement endpoint.
