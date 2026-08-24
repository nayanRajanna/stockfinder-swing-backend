
import os, time, hashlib, threading
from datetime import datetime, timedelta, timezone
from typing import Optional
import requests
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

GROWW_BASE="https://api.groww.in"
API_VERSION="1.0"

app=FastAPI(title="StockFinder Swing Backend", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

_lock=threading.Lock()
TOKEN=os.getenv("GROWW_ACCESS_TOKEN","").strip()
TOKEN_EXPIRY=os.getenv("GROWW_TOKEN_EXPIRY","")

def headers(token=None):
    return {"Accept":"application/json","Authorization":f"Bearer {token or TOKEN}","X-API-VERSION":API_VERSION}

def groww_get(path, params=None):
    if not TOKEN:
        raise HTTPException(503,"Groww access token is not configured")
    r=requests.get(GROWW_BASE+path,params=params,headers=headers(),timeout=15)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Groww API error: {r.text[:500]}")
    data=r.json()
    if data.get("status")=="FAILURE":
        raise HTTPException(502, data.get("error",{}).get("message","Groww request failed"))
    return data.get("payload",data)

def set_token(token, expiry=""):
    global TOKEN,TOKEN_EXPIRY
    with _lock:
        TOKEN=token.strip()
        TOKEN_EXPIRY=expiry or ""

class RefreshBody(BaseModel):
    api_key: Optional[str]=None
    api_secret: Optional[str]=None
    mode: str="approval"  # approval or totp
    totp: Optional[str]=None

def checksum(secret,timestamp):
    return hashlib.sha256((secret+timestamp).encode()).hexdigest()

@app.get("/api/health")
def health():
    return {"ok":True,"service":"StockFinder Swing","time":datetime.now(timezone.utc).isoformat()}

@app.get("/api/connector/status")
def connector_status():
    if not TOKEN:
        return {"status":"offline","last_refresh":None,"expiry":None}
    try:
        p=groww_get("/v1/user/detail")
        return {"status":"connected","last_refresh":datetime.now(timezone.utc).isoformat(),
                "expiry":TOKEN_EXPIRY or None,
                "nse_enabled":p.get("nse_enabled"),"bse_enabled":p.get("bse_enabled")}
    except Exception:
        return {"status":"expired_or_invalid","last_refresh":None,"expiry":TOKEN_EXPIRY or None}

@app.post("/api/connector/refresh")
def refresh(body: RefreshBody):
    key=body.api_key or os.getenv("GROWW_API_KEY","")
    secret=body.api_secret or os.getenv("GROWW_API_SECRET","")
    if not key: raise HTTPException(400,"API key is missing")
    if body.mode=="totp":
        if not body.totp: raise HTTPException(400,"TOTP code is required")
        payload={"key_type":"totp","totp":body.totp}
    else:
        if not secret: raise HTTPException(400,"API secret is missing")
        ts=str(int(time.time()))
        payload={"key_type":"approval","checksum":checksum(secret,ts),"timestamp":ts}
    r=requests.post(GROWW_BASE+"/v1/token/api/access",json=payload,
                    headers={"Authorization":f"Bearer {key}","Content-Type":"application/json","Accept":"application/json"},
                    timeout=15)
    if r.status_code>=400: raise HTTPException(r.status_code,r.text[:700])
    d=r.json()
    if d.get("status")=="FAILURE": raise HTTPException(502,d.get("error",{}).get("message","Token generation failed"))
    p=d.get("payload",d)
    token=p.get("token")
    if not token: raise HTTPException(502,"Groww did not return an access token")
    set_token(token,p.get("expiry",""))
    return {"status":"connected","expiry":p.get("expiry"),"token_ref_id":p.get("tokenRefId")}

def historical(symbol, exchange="NSE", days=380, interval=1440):
    end=datetime.now()
    start=end-timedelta(days=days)
    params={"exchange":exchange,"segment":"CASH","trading_symbol":symbol,
            "start_time":start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time":end.strftime("%Y-%m-%d %H:%M:%S"),
            "interval_in_minutes":str(interval)}
    p=groww_get("/v1/historical/candle/range",params)
    candles=p.get("candles",[])
    if not candles: raise HTTPException(404,f"No historical candles found for {exchange}:{symbol}")
    df=pd.DataFrame(candles,columns=["ts","open","high","low","close","volume"])
    for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
    df=df.dropna().reset_index(drop=True)
    return df

def quote(symbol,exchange="NSE"):
    return groww_get("/v1/live-data/quote",{"exchange":exchange,"segment":"CASH","trading_symbol":symbol})

def indicators(df):
    c=df.close; h=df.high; l=df.low; v=df.volume
    ema20=c.ewm(span=20,adjust=False).mean()
    ema50=c.ewm(span=50,adjust=False).mean()
    ema200=c.ewm(span=200,adjust=False).mean()
    delta=c.diff(); gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    rsi=100-(100/(1+(gain/loss.replace(0,np.nan))))
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/14,adjust=False).mean()
    macd=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    signal=macd.ewm(span=9,adjust=False).mean()
    vol20=v.rolling(20).mean()
    return {"close":float(c.iloc[-1]),"ema20":float(ema20.iloc[-1]),"ema50":float(ema50.iloc[-1]),
            "ema200":float(ema200.iloc[-1]),"rsi":float(rsi.iloc[-1]),
            "atr":float(atr.iloc[-1]),"macd":float(macd.iloc[-1]),"macd_signal":float(signal.iloc[-1]),
            "volume_ratio":float(v.iloc[-1]/vol20.iloc[-1]) if vol20.iloc[-1] else 0}

def patterns(df):
    x=df.tail(80).copy()
    last=x.iloc[-1]; prev=x.iloc[-2]
    patterns=[]
    recent_high=x.high.iloc[-21:-1].max()
    recent_low=x.low.iloc[-21:-1].min()
    if last.close>recent_high and last.volume>x.volume.iloc[-21:-1].mean()*1.2:
        patterns.append("Breakout")
    if last.close>last.open and prev.close<prev.open and last.open<=prev.close and last.close>=prev.open:
        patterns.append("Bullish Engulfing")
    # simple double-bottom: two similar lows separated by a rebound
    lows=x.low.values
    if len(lows)>=30:
        a=int(np.argmin(lows[:30])); b=30+int(np.argmin(lows[30:]))
        if b-a>=8 and abs(lows[a]-lows[b])/max(lows[a],1)<0.025:
            patterns.append("Double Bottom")
    # ascending triangle approximation
    if len(x)>=25:
        highs=x.high.tail(25); lows2=x.low.tail(25)
        if highs.max()-highs.min() < max(last.close*0.025,1) and lows2.iloc[-1]>lows2.iloc[0]:
            patterns.append("Ascending Triangle")
    if not patterns: patterns.append("Trend / Pullback")
    return patterns

def score(ind, pats, df):
    s=0; reasons=[]
    if ind["close"]>ind["ema20"]: s+=15; reasons.append("price above 20 EMA")
    if ind["ema20"]>ind["ema50"]: s+=15; reasons.append("20 EMA above 50 EMA")
    if ind["ema50"]>ind["ema200"]: s+=10; reasons.append("50 EMA above 200 EMA")
    if 50<=ind["rsi"]<=68: s+=15; reasons.append("RSI in bullish swing zone")
    elif 40<=ind["rsi"]<50: s+=7
    if ind["macd"]>ind["macd_signal"]: s+=15; reasons.append("MACD bullish")
    if ind["volume_ratio"]>=1.5: s+=15; reasons.append("volume expansion")
    elif ind["volume_ratio"]>=1.1: s+=8
    if any(p in pats for p in ["Breakout","Bullish Engulfing","Double Bottom","Ascending Triangle"]):
        s+=15; reasons.append(pats[0]+" pattern")
    return min(100,int(s)),reasons

@app.get("/api/scan/{symbol}")
def scan(symbol:str, exchange:str="NSE"):
    symbol=symbol.upper().strip()
    df=historical(symbol,exchange)
    ind=indicators(df); pats=patterns(df); s,reasons=score(ind,pats,df)
    q=quote(symbol,exchange)
    # Groww quote payload keys can evolve; keep several common aliases.
    price=q.get("ltp",q.get("last_price",ind["close"])) if isinstance(q,dict) else ind["close"]
    return {"symbol":symbol,"exchange":exchange,"price":price,"score":s,"pattern":", ".join(pats),
            "rsi":round(ind["rsi"],2),"volume_ratio":round(ind["volume_ratio"],2),
            "ema20":round(ind["ema20"],2),"ema50":round(ind["ema50"],2),"ema200":round(ind["ema200"],2),
            "macd":round(ind["macd"],4),"macd_signal":round(ind["macd_signal"],4),
            "atr":round(ind["atr"],2),"reasons":reasons,
            "candles_used":len(df)}
