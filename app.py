import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import List

import g4f

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# =====================================
# CONFIG
# =====================================

API_KEY = "sk-your-secret-key"

PROVIDERS = [
    g4f.Provider.DuckDuckGo,
    g4f.Provider.Blackbox,
]

executor = ThreadPoolExecutor(max_workers=5)

# =====================================
# APP
# =====================================

app = FastAPI(
    title="HuggingFace AI Gateway"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================
# MODELS
# =====================================

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: str = "gpt-4o-mini"
    messages: List[Message]
    stream: bool = False

# =====================================
# AUTH
# =====================================

def verify_api_key(auth: str):

    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization"
        )

    token = auth.replace("Bearer ", "")

    if token != API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid API Key"
        )

# =====================================
# GENERATE
# =====================================

def generate(model, messages):

    last_error = None

    for provider in PROVIDERS:

        try:

            response = g4f.ChatCompletion.create(
                model=model,
                provider=provider,
                messages=messages
            )

            return response

        except Exception as e:

            print(f"{provider.__name__}: {e}")

            last_error = e

    raise Exception(str(last_error))

# =====================================
# STREAM
# =====================================

async def stream_generate(model, messages):

    loop = asyncio.get_event_loop()

    for provider in PROVIDERS:

        try:

            response = await loop.run_in_executor(
                executor,
                lambda: g4f.ChatCompletion.create(
                    model=model,
                    provider=provider,
                    messages=messages,
                    stream=True
                )
            )

            for chunk in response:

                if chunk:

                    payload = {
                        "choices": [
                            {
                                "delta": {
                                    "content": chunk
                                }
                            }
                        ]
                    }

                    yield f"data: {json.dumps(payload)}\\n\\n"

            yield "data: [DONE]\\n\\n"

            return

        except Exception as e:

            print(f"Streaming Error: {e}")

            continue

    yield f"data: {json.dumps({'error':'All providers failed'})}\\n\\n"

# =====================================
# API
# =====================================

@app.get("/")
async def home():

    return {
        "status": "online"
    }

@app.post("/v1/chat/completions")
async def chat(
    req: ChatRequest,
    authorization: str = Header(None)
):

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization"
        )

    verify_api_key(authorization)

    messages = [
        m.model_dump()
        for m in req.messages
    ]

    # =================================
    # STREAM
    # =================================

    if req.stream:

        return StreamingResponse(
            stream_generate(
                req.model,
                messages
            ),
            media_type="text/event-stream"
        )

    # =================================
    # NORMAL
    # =================================

    loop = asyncio.get_event_loop()

    try:

        response = await loop.run_in_executor(
            executor,
            lambda: generate(
                req.model,
                messages
            )
        )

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": response
                    }
                }
            ]
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )