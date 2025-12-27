from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any, List
import requests
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
import aiohttp
import urllib3
from pydantic import BaseModel, Field
import uvicorn
from contextlib import asynccontextmanager
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Models
class APIResult(BaseModel):
    name: str
    status: str
    status_code: Optional[int] = None
    response: Optional[str] = None
    error: Optional[str] = None

class APIResponse(BaseModel):
    phone: str
    total_apis: int
    sent: int
    errors: int
    rate_limited: int
    skipped: int
    results: List[APIResult]

class HealthResponse(BaseModel):
    message: str
    status: str
    endpoints: Dict[str, str]
    example: str
    total_apis: int
    methods: List[str]

# Global variables to store config
apis_config: Dict = {}
variables: Dict = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config on startup and reload periodically"""
    load_config()
    
    # Start background task to reload config periodically
    task = asyncio.create_task(reload_config_periodically())
    
    yield
    
    # Cleanup on shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title="SMS API Service",
    description="Multiple API integration service for sending OTP requests",
    version="1.0.0",
    lifespan=lifespan
)

def load_config():
    """Load APIs configuration from JSON file"""
    global apis_config, variables
    try:
        with open('apis.json', 'r') as f:
            config = json.load(f)
        apis_config = config.get('apis', {})
        variables = config.get('variables', {})
        logger.info(f"Loaded {len(apis_config)} APIs from configuration")
    except FileNotFoundError:
        logger.error("Config file 'apis.json' not found")
        apis_config, variables = {}, {}
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config file: {e}")
        apis_config, variables = {}, {}
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        apis_config, variables = {}, {}

async def reload_config_periodically(interval: int = 300):
    """Periodically reload configuration"""
    while True:
        await asyncio.sleep(interval)
        load_config()

def build_api_list(mobile: str) -> List[Dict[str, Any]]:
    """Build API configurations with mobile number substitution"""
    apis = []
    
    for api_name, api_data in apis_config.items():
        api_entry = {
            "name": api_data.get('name', api_name),
            "url": api_data.get('url', ''),
            "method": api_data.get('method', 'post').lower(),
            "headers": {}
        }
        
        # Process headers
        headers = api_data.get('headers', {})
        for key, value in headers.items():
            if isinstance(value, str) and value in variables:
                api_entry['headers'][key] = variables[value]
            else:
                api_entry['headers'][key] = value
        
        # Process URL
        url = api_entry['url']
        if '{number}' in url:
            url = url.replace('{number}', mobile)
        api_entry['url'] = url
        
        # Process body
        if 'body' in api_data:
            body = api_data['body']
            if isinstance(body, str):
                body = body.replace('{number}', mobile)
                # Try to parse as JSON if it looks like JSON
                if (body.startswith('{') and body.endswith('}')) or (body.startswith('[') and body.endswith(']')):
                    try:
                        body = json.loads(body)
                    except:
                        pass
            api_entry['body'] = body
        
        apis.append(api_entry)
    
    return apis

async def call_api_sync(api_config: Dict[str, Any]) -> APIResult:
    """Call API synchronously (for use with thread pool)"""
    name = api_config.get('name', 'unknown')
    
    try:
        method = api_config.get('method', 'post')
        url = api_config['url']
        headers = api_config.get('headers', {})
        body = api_config.get('body', None)
        
        if not url:
            return APIResult(
                name=name,
                status="skipped",
                reason="No URL provided"
            )
        
        timeout = aiohttp.ClientTimeout(total=15)
        
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=False)) as session:
            if method == 'get':
                async with session.get(url, headers=headers) as response:
                    status_code = response.status
                    response_text = await response.text()
            else:
                content_type = headers.get('content-type', '').lower()
                if 'application/x-www-form-urlencoded' in content_type:
                    # For form-urlencoded, data should be a dict or a string
                    async with session.post(url, headers=headers, data=body) as response:
                        status_code = response.status
                        response_text = await response.text()
                elif isinstance(body, (dict, list)):
                    # If body is already a dict/list, use json= which auto-encodes it
                    async with session.post(url, headers=headers, json=body) as response:
                        status_code = response.status
                        response_text = await response.text()
                else:
                    # Fallback for string bodies or empty bodies
                    async with session.post(url, headers=headers, data=body) as response:
                        status_code = response.status
                        response_text = await response.text()
        
        # Determine status based on status code
        if status_code in [200, 201, 202]:
            result_status = "sent"
        elif status_code in [400, 401, 403]:
            result_status = "auth_error"
        elif status_code == 404:
            result_status = "not_found"
        elif status_code == 429:
            result_status = "rate_limited"
        elif status_code == 500:
            result_status = "server_error"
        elif status_code >= 400:
            result_status = "error"
        else:
            result_status = "sent"
        
        return APIResult(
            name=name,
            status=result_status,
            status_code=status_code,
            response=response_text[:150] if response_text else None
        )
        
    except asyncio.TimeoutError:
        return APIResult(
            name=name,
            status="timeout",
            error="Request timeout (15s)"
        )
    except aiohttp.ClientConnectionError:
        return APIResult(
            name=name,
            status="connection_error",
            error="Connection failed"
        )
    except Exception as e:
        return APIResult(
            name=name,
            status="error",
            error=str(e)[:150]
        )

async def call_api_concurrent(api_config: Dict[str, Any]) -> APIResult:
    """Wrapper for async API call"""
    return await call_api_sync(api_config)

@app.get("/api/v1", response_model=APIResponse)
async def api_v1(
    phone: Optional[str] = Query(None, alias="phone", description="Phone number"),
    mobile: Optional[str] = Query(None, alias="mobile", description="Phone number (alternative)")
):
    """
    Send OTP requests to multiple APIs for a given phone number
    
    - **phone/mobile**: The phone number to send OTP to
    """
    mobile_number = phone or mobile
    
    if not mobile_number:
        raise HTTPException(
            status_code=400,
            detail="Missing phone number. Use ?phone=NUMBER or ?mobile=NUMBER"
        )
    
    # Build API list
    apis = build_api_list(mobile_number)
    
    # Call APIs concurrently
    tasks = [call_api_concurrent(api) for api in apis]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results
    processed_results = []
    for result in results:
        if isinstance(result, Exception):
            processed_results.append(APIResult(
                name="unknown",
                status="error",
                error=str(result)[:150]
            ))
        else:
            processed_results.append(result)
    
    # Calculate statistics
    sent_count = sum(1 for r in processed_results if r.status == 'sent')
    error_count = sum(1 for r in processed_results if r.status in ['error', 'auth_error', 'not_found', 'server_error', 'connection_error', 'timeout'])
    rate_limited = sum(1 for r in processed_results if r.status == 'rate_limited')
    skipped = sum(1 for r in processed_results if r.status == 'skipped')
    
    return APIResponse(
        phone=mobile_number,
        total_apis=len(apis),
        sent=sent_count,
        errors=error_count,
        rate_limited=rate_limited,
        skipped=skipped,
        results=processed_results
    )

@app.get("/", response_model=HealthResponse)
async def home():
    """Root endpoint with service information"""
    return HealthResponse(
        message="SMS API Service - Multiple APIs",
        status="running",
        endpoints={
            "/api/v1": "GET - Send OTP requests. Parameters: ?phone=NUMBER or ?mobile=NUMBER",
            "/docs": "API Documentation",
            "/redoc": "Alternative Documentation"
        },
        example="/api/v1?phone=01812345678",
        total_apis=len(apis_config),
        methods=["GET"]
    )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "apis_loaded": len(apis_config),
        "timestamp": asyncio.get_event_loop().time()
    }

@app.get("/reload-config")
async def reload_config_endpoint():
    """Manually reload configuration"""
    load_config()
    return {"message": "Configuration reloaded", "apis_count": len(apis_config)}

# Alternative synchronous version using ThreadPoolExecutor (if needed)
async def call_apis_sync_version(apis: List[Dict[str, Any]]) -> List[APIResult]:
    """Alternative: Use ThreadPoolExecutor for CPU-bound or blocking operations"""
    def call_api_sync_wrapper(api_config):
        """Synchronous wrapper for requests library"""
        name = api_config.get('name', 'unknown')
        try:
            method = api_config.get('method', 'post').lower()
            url = api_config['url']
            headers = api_config.get('headers', {})
            body = api_config.get('body', None)
            
            if not url:
                return APIResult(
                    name=name,
                    status="skipped",
                    reason="No URL provided"
                )
            
            if method == 'get':
                response = requests.get(url, headers=headers, timeout=15, verify=False)
            else:
                if isinstance(body, str) and headers.get('content-type') == 'application/x-www-form-urlencoded':
                    response = requests.post(url, headers=headers, data=body, timeout=15, verify=False)
                elif body:
                    response = requests.post(url, headers=headers, data=body, timeout=15, verify=False)
                else:
                    response = requests.post(url, headers=headers, timeout=15, verify=False)
            
            status_code = response.status_code
            
            if status_code in [200, 201, 202]:
                result_status = "sent"
            elif status_code in [400, 401, 403]:
                result_status = "auth_error"
            elif status_code == 404:
                result_status = "not_found"
            elif status_code == 429:
                result_status = "rate_limited"
            elif status_code == 500:
                result_status = "server_error"
            elif status_code >= 400:
                result_status = "error"
            else:
                result_status = "sent"
            
            return APIResult(
                name=name,
                status=result_status,
                status_code=status_code,
                response=response.text[:150] if response.text else None
            )
            
        except requests.exceptions.Timeout:
            return APIResult(
                name=name,
                status="timeout",
                error="Request timeout (15s)"
            )
        except requests.exceptions.ConnectionError:
            return APIResult(
                name=name,
                status="connection_error",
                error="Connection failed"
            )
        except Exception as e:
            return APIResult(
                name=name,
                status="error",
                error=str(e)[:150]
            )
    
    # Use ThreadPoolExecutor for concurrent synchronous calls
    with ThreadPoolExecutor(max_workers=20) as executor:
        loop = asyncio.get_event_loop()
        tasks = [loop.run_in_executor(executor, call_api_sync_wrapper, api) for api in apis]
        results = await asyncio.gather(*tasks)
    
    return results

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=5000,
        log_level="info",
        access_log=True
    )