from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any, List
import requests
import json
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from datetime import datetime
import time
import psutil
import os
from typing import Dict
import os
from dotenv import load_dotenv
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
port = os.getenv('PORT')
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
# Store startup time
startup_time = time.time()

def get_system_info() -> Dict:
    """Get system information"""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    return {
        "cpu_percent": cpu_percent,
        "memory_total": memory.total / (1024**3),  # Convert to GB
        "memory_used": memory.used / (1024**3),
        "memory_percent": memory.percent,
        "disk_total": disk.total / (1024**3),
        "disk_used": disk.used / (1024**3),
        "disk_percent": disk.percent
    }

def get_uptime() -> str:
    """Calculate and format uptime"""
    uptime_seconds = time.time() - startup_time
    
    # Convert to days, hours, minutes, seconds
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    seconds = int(uptime_seconds % 60)
    
    if days > 0:
        return f"{days}d {hours}h {minutes}m {seconds}s"
    elif hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

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
@app.get("/healthz", response_class=HTMLResponse)
async def health_check(request: Request):
    """Beautiful HTML health check endpoint"""
    
    # Calculate response time
    start_time = time.time()
    
    # Get system info
    system_info = get_system_info()
    uptime = get_uptime()
    
    # Calculate response time
    response_time = (time.time() - start_time) * 1000  # Convert to ms
    
    # Get current timestamp
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Determine status color based on response time
    if response_time < 100:
        status_color = "#10B981"  # Green
        status_text = "Excellent"
    elif response_time < 300:
        status_color = "#F59E0B"  # Yellow
        status_text = "Good"
    else:
        status_color = "#EF4444"  # Red
        status_text = "Slow"
    
    # Determine CPU color
    cpu_color = "#10B981" if system_info["cpu_percent"] < 70 else "#EF4444"
    # Determine Memory color
    mem_color = "#10B981" if system_info["memory_percent"] < 80 else "#EF4444"
    # Determine Disk color
    disk_color = "#10B981" if system_info["disk_percent"] < 80 else "#EF4444"
    
    # HTML with beautiful design
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Health Dashboard</title>
        <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            
            body {{
                font-family: 'Poppins', sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                padding: 20px;
            }}
            
            .dashboard {{
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(10px);
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                padding: 40px;
                width: 100%;
                max-width: 800px;
            }}
            
            .header {{
                text-align: center;
                margin-bottom: 40px;
            }}
            
            .header h1 {{
                color: #333;
                font-size: 2.5rem;
                font-weight: 700;
                margin-bottom: 10px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            
            .status-badge {{
                display: inline-block;
                padding: 8px 20px;
                border-radius: 50px;
                font-weight: 600;
                font-size: 1.2rem;
                margin: 10px 0;
                background-color: {status_color};
                color: white;
            }}
            
            .metrics-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}
            
            .metric-card {{
                background: white;
                border-radius: 15px;
                padding: 25px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.1);
                transition: transform 0.3s ease;
            }}
            
            .metric-card:hover {{
                transform: translateY(-5px);
            }}
            
            .metric-title {{
                font-size: 0.9rem;
                color: #666;
                text-transform: uppercase;
                letter-spacing: 1px;
                margin-bottom: 10px;
                font-weight: 600;
            }}
            
            .metric-value {{
                font-size: 2rem;
                font-weight: 700;
                color: #333;
                margin-bottom: 5px;
            }}
            
            .metric-subtext {{
                font-size: 0.9rem;
                color: #888;
            }}
            
            .progress-bar {{
                width: 100%;
                height: 8px;
                background: #e0e0e0;
                border-radius: 4px;
                margin-top: 15px;
                overflow: hidden;
            }}
            
            .progress-fill {{
                height: 100%;
                border-radius: 4px;
                transition: width 0.3s ease;
            }}
            
            .info-section {{
                background: #f8f9fa;
                border-radius: 15px;
                padding: 25px;
                margin-top: 30px;
            }}
            
            .info-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
            }}
            
            .info-item {{
                display: flex;
                justify-content: space-between;
                padding: 10px 0;
                border-bottom: 1px solid #e0e0e0;
            }}
            
            .info-label {{
                color: #666;
                font-weight: 500;
            }}
            
            .info-value {{
                color: #333;
                font-weight: 600;
            }}
            
            .timestamp {{
                text-align: center;
                color: #888;
                font-size: 0.9rem;
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #e0e0e0;
            }}
            
            @media (max-width: 768px) {{
                .dashboard {{
                    padding: 20px;
                }}
                
                .header h1 {{
                    font-size: 2rem;
                }}
                
                .metrics-grid {{
                    grid-template-columns: 1fr;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="dashboard">
            <div class="header">
                <h1>🏥 Health Dashboard</h1>
                <div class="status-badge">{status_text}</div>
                <p>Real-time system monitoring and performance metrics</p>
            </div>
            
            <div class="metrics-grid">
                <div class="metric-card">
                    <div class="metric-title">Response Time</div>
                    <div class="metric-value" style="color: {status_color};">{response_time:.2f} ms</div>
                    <div class="metric-subtext">API Processing Time</div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: {min(response_time/10, 100)}%; background: {status_color};"></div>
                    </div>
                </div>
                
                <div class="metric-card">
                    <div class="metric-title">System Uptime</div>
                    <div class="metric-value">{uptime}</div>
                    <div class="metric-subtext">Continuous Operation</div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: 100%; background: #10B981;"></div>
                    </div>
                </div>
            </div>
            
            <div class="metrics-grid">
                <div class="metric-card">
                    <div class="metric-title">CPU Usage</div>
                    <div class="metric-value" style="color: {cpu_color};">{system_info["cpu_percent"]:.1f}%</div>
                    <div class="metric-subtext">Processor Load</div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: {system_info["cpu_percent"]}%; background: {cpu_color};"></div>
                    </div>
                </div>
                
                <div class="metric-card">
                    <div class="metric-title">Memory Usage</div>
                    <div class="metric-value" style="color: {mem_color};">{system_info["memory_percent"]:.1f}%</div>
                    <div class="metric-subtext">{system_info["memory_used"]:.1f} GB / {system_info["memory_total"]:.1f} GB</div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: {system_info["memory_percent"]}%; background: {mem_color};"></div>
                    </div>
                </div>
                
                <div class="metric-card">
                    <div class="metric-title">Disk Usage</div>
                    <div class="metric-value" style="color: {disk_color};">{system_info["disk_percent"]:.1f}%</div>
                    <div class="metric-subtext">{system_info["disk_used"]:.1f} GB / {system_info["disk_total"]:.1f} GB</div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: {system_info["disk_percent"]}%; background: {disk_color};"></div>
                    </div>
                </div>
            </div>
            
            <div class="info-section">
                <div class="info-grid">
                    <div class="info-item">
                        <span class="info-label">Service Status:</span>
                        <span class="info-value" style="color: {status_color};">Operational ✓</span>
                    </div>
                    <div class="info-item">
                        <span class="info-label">Requests Handled:</span>
                        <span class="info-value">Active</span>
                    </div>
                    <div class="info-item">
                        <span class="info-label">Environment:</span>
                        <span class="info-value">{os.getenv("ENVIRONMENT", "development").title()}</span>
                    </div>
                    <div class="info-item">
                        <span class="info-label">Server Time:</span>
                        <span class="info-value">{current_time}</span>
                    </div>
                </div>
            </div>
            
            <div class="timestamp">
                Last checked: {current_time} | Auto-refresh every 30 seconds
            </div>
        </div>
        
        <script>
            // Auto-refresh the page every 30 seconds
            setTimeout(function() {{
                location.reload();
            }}, 30000);
            
            // Add some animations
            document.addEventListener('DOMContentLoaded', function() {{
                const cards = document.querySelectorAll('.metric-card');
                cards.forEach((card, index) => {{
                    card.style.opacity = '0';
                    card.style.transform = 'translateY(20px)';
                    
                    setTimeout(() => {{
                        card.style.transition = 'opacity 0.5s ease, transform 0.5s ease';
                        card.style.opacity = '1';
                        card.style.transform = 'translateY(0)';
                    }}, index * 100);
                }});
            }});
        </script>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)


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
        port=port,
        log_level="info",
        access_log=True

    )



