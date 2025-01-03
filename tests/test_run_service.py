# -*- coding: utf-8 -*-
"""Test run_service.py script using pytest for reliable automation."""

import re
import sys
import logging
import pexpect
import os
import time
import pytest
import tempfile
import shutil
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from termcolor import colored
from colorama import init
from web3 import Web3
from eth_account import Account
import requests
import docker
from dotenv import load_dotenv
from operate.constants import HEALTH_CHECK_URL

# Initialize colorama and load environment
init()
load_dotenv()

STARTUP_WAIT = 10
SERVICE_INIT_WAIT = 60
CONTAINER_STOP_WAIT = 20

# Handle the distutils warning
os.environ['SETUPTOOLS_USE_DISTUTILS'] = 'stdlib'

def get_service_config(config_path: str) -> dict:
    """Get service-specific configuration."""
    if "modius" in config_path.lower():
        return {
            "container_name": "optimus",  # Update with actual Modius container name
            "health_check_url": HEALTH_CHECK_URL,  # Update with actual Modius health endpoint
        }
    else:
        # Default PredictTrader config
        return {
            "container_name": "traderpearl",
            "health_check_url": HEALTH_CHECK_URL,
        }

def check_docker_status(logger: logging.Logger, config_path: str) -> bool:
    """Check if Docker containers are running properly."""
    service_config = get_service_config(config_path)
    container_name = service_config["container_name"]
    
    max_retries = 3
    retry_delay = 20
    
    for attempt in range(max_retries):
        logger.info(f"Checking Docker status (attempt {attempt + 1}/{max_retries})")
        try:
            client = docker.from_env()
            
            # Check all containers, including stopped ones
            all_containers = client.containers.list(all=True, filters={"name": container_name})
            running_containers = client.containers.list(filters={"name": container_name})
            
            if not all_containers:
                logger.error(f"No {container_name} containers found (attempt {attempt + 1}/{max_retries})")
                if attempt == max_retries - 1:
                    return False
                logger.info(f"Waiting {retry_delay} seconds before retry...")
                time.sleep(retry_delay)
                continue
            
            # Log status of all containers
            for container in all_containers:
                logger.info(f"Container {container.name} status: {container.status}")
                
                if container.status == "exited":
                    # Get exit code
                    inspect = client.api.inspect_container(container.id)
                    exit_code = inspect['State']['ExitCode']
                    logger.error(f"Container {container.name} exited with code {exit_code}")
                    
                    # Get last logs
                    logs = container.logs(tail=50).decode('utf-8')
                    logger.error(f"Container logs:\n{logs}")
                
                elif container.status == "restarting":
                    logger.error(f"Container {container.name} is restarting. Last logs:")
                    logs = container.logs(tail=50).decode('utf-8')
                    logger.error(f"Container logs:\n{logs}")
            
            # Check if all required containers are running
            if not running_containers:
                if attempt == max_retries - 1:
                    return False
                logger.info(f"Waiting {retry_delay} seconds for containers to start...")
                time.sleep(retry_delay)
                continue
            
            # Verify all running containers are actually running
            all_running = all(c.status == "running" for c in running_containers)
            if all_running:
                logger.info(f"All {container_name} containers are running")
                return True
            
            if attempt == max_retries - 1:
                return False
                
            logger.info(f"Some containers not running, waiting {retry_delay} seconds...")
            time.sleep(retry_delay)
            
        except Exception as e:
            logger.error(f"Error checking Docker status: {str(e)}")
            if attempt == max_retries - 1:
                return False
            logger.info(f"Waiting {retry_delay} seconds before retry...")
            time.sleep(retry_delay)
    
    return False

def check_service_health(logger: logging.Logger, config_path: str) -> tuple[bool, dict]:
    """Enhanced service health check with metrics."""
    service_config = get_service_config(config_path)
    health_check_url = service_config["health_check_url"]
    
    metrics = {
        'response_time': None,
        'status_code': None,
        'error': None,
        'successful_checks': 0,
        'total_checks': 0
    }
    
    start_monitoring = time.time()
    while time.time() - start_monitoring < 120:  # Run for 2 minutes
        try:
            metrics['total_checks'] += 1
            start_time = time.time()
            response = requests.get(health_check_url, timeout=10)
            metrics['response_time'] = time.time() - start_time
            metrics['status_code'] = response.status_code
            
            if response.status_code == 200:
                metrics['successful_checks'] += 1
                logger.info(f"Health check passed (response time: {metrics['response_time']:.2f}s)")
            else:
                logger.error(f"Health check failed - Status: {response.status_code}")
                return False, metrics
                
        except requests.exceptions.Timeout:
            metrics['error'] = 'timeout'
            logger.error("Health check timeout")
            return False, metrics
        except requests.exceptions.ConnectionError as e:
            metrics['error'] = 'connection_error'
            logger.error(f"Connection error: {str(e)}")
            return False, metrics
        except Exception as e:
            metrics['error'] = str(e)
            logger.error(f"Unexpected error in health check: {str(e)}")
            return False, metrics
            
        # Wait for remaining time in 5-second interval
        elapsed = time.time() - start_time
        if elapsed < 5:
            time.sleep(5 - elapsed)
    
    logger.info(f"Health check completed successfully - {metrics['successful_checks']} checks passed")
    return True, metrics

def check_shutdown_logs(logger: logging.Logger) -> bool:
    """Check shutdown logs for errors."""
    try:
        client = docker.from_env()
        containers = client.containers.list(filters={"name": "traderpearl"})
        
        for container in containers:
            logs = container.logs().decode('utf-8')
            if "Error during shutdown" in logs or "Failed to gracefully stop" in logs:
                logger.error(f"Found shutdown errors in container {container.name} logs")
                return False
                
        logger.info("Shutdown logs check passed")
        return True
    except Exception as e:
        logger.error(f"Error checking shutdown logs: {str(e)}")
        return False

def get_token_config():
    """Get token configurations"""
    return {
        "USDC": {
            "address": "0xd988097fb8612cc24eeC14542bC03424c656005f",
            "decimals": 6
        },
        # Add other tokens as needed
        "OLAS": {
            "address": "your_olas_address",
            "decimals": 18
        }
    }    

def handle_erc20_funding(output: str, logger: logging.Logger, rpc_url: str) -> str:
    """Handle funding requirement using Tenderly API for ERC20 tokens."""
    pattern = r"Please make sure Master (?:EOA|Safe) (0x[a-fA-F0-9]{40}) has at least ([0-9.]+) ([A-Z]+)"
    
    match = re.search(pattern, output)
    if match:
        wallet_address = match.group(1)
        required_amount = float(match.group(2))
        token_symbol = match.group(3)
        
        # Get token configuration
        token_configs = get_token_config()
        if token_symbol not in token_configs:
            raise Exception(f"Token {token_symbol} not configured")
            
        token_config = token_configs[token_symbol]
        token_address = token_config["address"]
        decimals = token_config["decimals"]
        
        try:
            # Convert amount to token units based on decimals
            amount_in_units = int(required_amount * (10 ** decimals))
            amount_hex = hex(amount_in_units)
            
            headers = {"Content-Type": "application/json"}
            payload = {
                "jsonrpc": "2.0",
                "method": "tenderly_setErc20Balance",
                "params": [token_address, wallet_address, amount_hex],
                "id": "1"
            }
            
            response = requests.post(rpc_url, headers=headers, json=payload)
            
            if response.status_code == 200:
                result = response.json()
                if 'error' in result:
                    raise Exception(f"Tenderly API error: {result['error']}")
                    
                logger.info(f"Successfully funded {required_amount} {token_symbol} to {wallet_address}")
                
                # Verify balance using Web3
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc_url))
                    erc20_abi = [
                        {
                            "constant": True,
                            "inputs": [{"name": "_owner", "type": "address"}],
                            "name": "balanceOf",
                            "outputs": [{"name": "balance", "type": "uint256"}],
                            "type": "function"
                        }
                    ]
                    token_contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=erc20_abi)
                    new_balance = token_contract.functions.balanceOf(wallet_address).call()
                    logger.info(f"New balance: {new_balance / (10 ** decimals)} {token_symbol}")
                except Exception as e:
                    logger.warning(f"Could not verify balance: {str(e)}")
                
                return ""
            else:
                raise Exception(f"Tenderly API request failed with status {response.status_code}")
                
        except Exception as e:
            logger.error(f"Failed to fund {token_symbol}: {str(e)}")
            raise
    
    return ""

def create_token_funding_handler(rpc_url: str):
    """Create a token funding handler with the specified RPC URL."""
    def handler(output: str, logger: logging.Logger) -> str:
        return handle_erc20_funding(output, logger, rpc_url)
    return handler

def handle_native_funding(output: str, logger: logging.Logger, rpc_url: str, config_type: str = "") -> str:
    """Handle funding requirement using Tenderly API for any native token."""
    patterns = [
        r"Please make sure Master EOA (0x[a-fA-F0-9]{40}) has at least (\d+\.\d+) (?:ETH|xDAI)",
        r"Please make sure Master Safe (0x[a-fA-F0-9]{40}) has at least (\d+\.\d+) (?:ETH|xDAI)"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            wallet_address = match.group(1)
            required_amount = float(match.group(2))
            wallet_type = "EOA" if "EOA" in pattern else "Safe"
            
            # Add buffer for Modius
            if "modius" in config_type.lower():
                original_amount = required_amount
                required_amount = 0.6  # Fixed amount for Modius
                logger.info(f"Modius detected: Increasing funding from {original_amount} ETH to {required_amount} ETH for gas buffer")
            
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url))
                amount_wei = w3.to_wei(required_amount, 'ether')
                amount_hex = hex(amount_wei)
                
                headers = {"Content-Type": "application/json"}
                payload = {
                    "jsonrpc": "2.0",
                    "method": "tenderly_addBalance",
                    "params": [wallet_address, amount_hex],
                    "id": "1"
                }
                
                response = requests.post(rpc_url, headers=headers, json=payload)
                
                if response.status_code == 200:
                    result = response.json()
                    if 'error' in result:
                        raise Exception(f"Tenderly API error: {result['error']}")
                        
                    # Get token name from the chain ID
                    chain_id = w3.eth.chain_id
                    token_name = "ETH" if chain_id in [1, 5, 11155111] else "xDAI"
                    
                    logger.info(f"Successfully funded {required_amount} {token_name} to {wallet_type} {wallet_address}")
                    new_balance = w3.eth.get_balance(wallet_address)
                    logger.info(f"New balance: {w3.from_wei(new_balance, 'ether')} {token_name}")
                    return ""
                else:
                    raise Exception(f"Tenderly API request failed with status {response.status_code}")
                    
            except Exception as e:
                logger.error(f"Failed to fund {wallet_type}: {str(e)}")
                raise
    
    return ""

def create_funding_handler(rpc_url: str, config_type: str):
    """Create a funding handler with the specified RPC URL and config type."""
    def handler(output: str, logger: logging.Logger) -> str:
        return handle_native_funding(output, logger, rpc_url, config_type)
    return handler

class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors."""
    def format(self, record):
        is_input = getattr(record, 'is_input', False)
        is_expect = getattr(record, 'is_expect', False)
        
        if is_input:
            record.msg = colored(record.msg, 'yellow')
        elif is_expect:
            record.msg = colored(record.msg, 'cyan')
        else:
            record.msg = colored(record.msg, 'green')
        
        return super().format(record)

def setup_logging(log_file: Optional[Path] = None) -> logging.Logger:
    """Set up logging configuration."""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    logger = logging.getLogger('test_runner')
    logger.setLevel(logging.DEBUG)
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_formatter = ColoredFormatter(
        '%(asctime)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    if log_file:
        log_path = logs_dir / log_file
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    
    return logger

def get_config_files():
    """Dynamically get all JSON config files from configs directory."""
    config_dir = Path("configs")
    if not config_dir.exists():
        raise FileNotFoundError("configs directory not found")
        
    config_files = list(config_dir.glob("*.json"))
    if not config_files:
        raise FileNotFoundError("No JSON config files found in configs directory")
    
    # Log found configs
    logger = logging.getLogger('test_runner')
    logger.info(f"Found config files: {[f.name for f in config_files]}")
    
    return [str(f) for f in config_files]

def get_config_specific_settings(config_path: str) -> dict:
    """Get config specific prompts and test settings."""

    if "modius" in config_path.lower():
        # Modius specific settings
        test_config = {
            "RPC_URL": os.getenv('MODIUS_RPC_URL'),
            "BACKUP_WALLET": os.getenv('MODIUS_BACKUP_WALLET', ''),
            "TEST_PASSWORD": os.getenv('MODIUS_TEST_PASSWORD', ''),
            "STAKING_CHOICE": os.getenv('MODIUS_STAKING_CHOICE', '1')
        }

        funding_handler = create_funding_handler(test_config["RPC_URL"], "modius")
        token_funding_handler = create_token_funding_handler(test_config["RPC_URL"])

        # Modius specific prompts
        prompts = {
            r"eth_newFilter \[hidden input\]": test_config["RPC_URL"],
            "input your password": test_config["TEST_PASSWORD"],
            "confirm your password": test_config["TEST_PASSWORD"],
            "Enter your choice": test_config["STAKING_CHOICE"],
            "backup owner": test_config["BACKUP_WALLET"],
            "Press enter to continue": "\n",
            "press enter": "\n",
            r"Enter local user account password \[hidden input\]": test_config["TEST_PASSWORD"],
            "Please enter Tenderly":"\n",
            "Please enter Coingecko API Key":"\n",
            r"Please make sure Master (EOA|Safe) .*has at least.*(?:ETH|xDAI)": funding_handler,
            r"Please make sure Master (?:EOA|Safe) .*has at least.*(?:USDC|OLAS)": token_funding_handler,
        }
        
    else:
        # Use existing PredictTrader settings
        test_config = TEST_CONFIG
        funding_handler = create_funding_handler(test_config["RPC_URL"], "predict_trader")
        prompts = {
            r"eth_newFilter \[hidden input\]": test_config["RPC_URL"],
            "input your password": test_config["TEST_PASSWORD"],
            "confirm your password": test_config["TEST_PASSWORD"],
            "Enter your choice": test_config["STAKING_CHOICE"],
            "backup owner": test_config["BACKUP_WALLET"],
            "Press enter to continue": "\n",
            "press enter": "\n",
            r"Please make sure Master (EOA|Safe) .*has at least.*(?:ETH|xDAI)": funding_handler,
            r"Enter local user account password \[hidden input\]": test_config["TEST_PASSWORD"]
        }

    return {"prompts": prompts, "test_config": test_config}

# Test Configuration
TEST_CONFIG = {
    "RPC_URL": os.getenv('RPC_URL', ''),
    "BACKUP_WALLET": os.getenv('BACKUP_WALLET', '0x4e9a8fE0e0499c58a53d3C2A2dE25aaCF9b925A8'),
    "TEST_PASSWORD": os.getenv('TEST_PASSWORD', ''),
    "STAKING_CHOICE": os.getenv('STAKING_CHOICE', '1')
}

# # Expected prompts and their responses for PredictTrader
# PROMPTS = {
#     r"eth_newFilter \[hidden input\]": TEST_CONFIG["RPC_URL"],
#     "input your password": TEST_CONFIG["TEST_PASSWORD"],
#     "confirm your password": TEST_CONFIG["TEST_PASSWORD"],
#     "Enter your choice": TEST_CONFIG["STAKING_CHOICE"],
#     "backup owner": TEST_CONFIG["BACKUP_WALLET"],
#     "Press enter to continue": "\n",
#     "press enter": "\n",
#     "Please make sure Master (EOA|Safe) .*has at least.*xDAI": funding_handler,
#     r"Enter local user account password \[hidden input\]": TEST_CONFIG["TEST_PASSWORD"]
# }

class BaseTestService:
    """Base test service class containing core test logic."""
    config_path = None
    config_settings = None
    logger = None
    _setup_complete = False  # Add class variable to track setup state

    @classmethod
    def setup_class(cls):
        """Setup for all tests"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        cls.log_file = Path(f'test_run_service_{timestamp}.log')
        cls.logger = setup_logging(cls.log_file)
        
        # Load config specific settings
        cls.config_settings = get_config_specific_settings(cls.config_path)
        cls.logger.info(f"Loaded settings for config: {cls.config_path}")
        
        # Create temporary directory and store original path
        cls.original_cwd = os.getcwd()
        cls.temp_dir = tempfile.TemporaryDirectory(prefix='operate_test_')

        cls._setup_complete = True
        
        # Copy project files
        exclude_patterns = [
            '.git',
            '.pytest_cache',
            '__pycache__',
            '*.pyc',
            '.operate',
            'logs',
            '*.log',
            '.env'
        ]
        
        def ignore_patterns(path, names):
            return set(n for n in names if any(p in n or any(p.endswith(n) for p in exclude_patterns) for p in exclude_patterns))
        
        shutil.copytree(cls.original_cwd, cls.temp_dir.name, dirs_exist_ok=True, ignore=ignore_patterns)
        
        git_dir = Path(cls.original_cwd) / '.git'
        if git_dir.exists():
            shutil.copytree(git_dir, Path(cls.temp_dir.name) / '.git', symlinks=True)    
            
        os.chdir(cls.temp_dir.name)
        
        cls._setup_environment()
        
        # Start the service
        cls.start_service()
        time.sleep(STARTUP_WAIT)

    @classmethod
    def _setup_environment(cls):
        """Setup environment for tests"""
        cls.logger.info("Setting up test environment...")

        venv_path = os.environ.get('VIRTUAL_ENV')
        
        # Create a clean environment without virtualenv variables
        cls.temp_env = os.environ.copy()
        cls.temp_env.pop('VIRTUAL_ENV', None)
        cls.temp_env.pop('POETRY_ACTIVE', None)
        
        if venv_path:
            # Get site-packages path
            if os.name == 'nt':  # Windows
                site_packages = Path(venv_path) / 'Lib' / 'site-packages'
            else:  # Unix-like
                site_packages = list(Path(venv_path).glob('lib/python*/site-packages'))[0]
                
            # Add site-packages to PYTHONPATH
            pythonpath = cls.temp_env.get('PYTHONPATH', '')
            cls.temp_env['PYTHONPATH'] = f"{site_packages}:{pythonpath}" if pythonpath else str(site_packages)
            
            # Remove virtualenv path from PATH
            paths = cls.temp_env['PATH'].split(os.pathsep)
            paths = [p for p in paths if not p.startswith(str(venv_path))]
            cls.temp_env['PATH'] = os.pathsep.join(paths)
            
        else:
            cls.logger.warning("No virtualenv detected")

        cls.logger.info("Environment setup completed")
    
    @classmethod
    def teardown_class(cls):
        """Cleanup after all tests"""
        try:
            cls.logger.info("Starting test cleanup...")
            os.chdir(cls.original_cwd)
            cls.temp_dir.cleanup()
            cls.logger.info("Cleanup completed successfully")
            cls._setup_complete = False

        except Exception as e:
            cls.logger.error(f"Error during cleanup: {str(e)}")

    @classmethod
    def start_service(cls):
        """Start the service and handle initial setup."""
        try:
            cls.logger.info(f"Starting run_service.py test with config: {cls.config_path}")
            
            # Start the process with pexpect
            cls.child = pexpect.spawn(
                f'bash ./run_service.sh {cls.config_path}',
                encoding='utf-8',
                timeout=600,
                env=cls.temp_env,
                cwd="."
            )
            
            cls.child.logfile = sys.stdout
            
            # Handle the interaction using config specific prompts
            try:
                while True:
                    patterns = list(cls.config_settings["prompts"].keys())
                    index = cls.child.expect(patterns, timeout=600)
                    pattern = patterns[index]
                    response = cls.config_settings["prompts"][pattern]
                
                    cls.logger.info(f"Matched prompt: {pattern}", extra={'is_expect': True})

                    if callable(response):
                        output = cls.child.before + cls.child.after
                        response = response(output, cls.logger)

                    if "password" in pattern.lower():
                        cls.logger.info("Sending: [HIDDEN]", extra={'is_input': True})
                    else:
                        cls.logger.info(f"Sending: {response}", extra={'is_input': True})
                    
                    cls.child.sendline(response)
                    
            except pexpect.EOF:
                cls.logger.info("Initial setup completed")
                
                # Add delay to ensure services are up
                time.sleep(SERVICE_INIT_WAIT)
                
                # Verify Docker containers are running
           
                retries = 5
                while retries > 0:
                    if check_docker_status(cls.logger, cls.config_path):
                        break
                    time.sleep(CONTAINER_STOP_WAIT)
                    retries -= 1

                if retries == 0:
                    # Get service config to use in error message
                    service_config = get_service_config(cls.config_path)
                    container_name = service_config["container_name"]
                    raise Exception(f"{container_name} containers failed to start")
                    
            except Exception as e:
                cls.logger.error(f"Error in setup: {str(e)}")
                raise
                
        except Exception as e:
            cls.logger.error(f"Service start failed: {str(e)}")
            raise

    @classmethod
    def stop_service(cls):
        """Stop the service"""
        cls.logger.info("Stopping service...")
        process = pexpect.spawn(f'bash ./stop_service.sh {cls.config_path}', encoding='utf-8', timeout=30)
        process.expect(pexpect.EOF)
        time.sleep(30)

@pytest.mark.parametrize('config_path', get_config_files())
class TestAgentService:
    """Test class that runs tests for all configs."""
    config_path = None

    @pytest.fixture(autouse=True)
    def setup_test(self, config_path):
        """Setup before each test method if it's the first test"""
        TestAgentService.config_path = config_path
        if not BaseTestService._setup_complete:
            BaseTestService.config_path = config_path
            BaseTestService.setup_class()
        yield
        # No teardown here - let the last test handle it

    def test_01_health_check(self):
        """Test service health endpoint"""
        BaseTestService.logger.info("Testing service health...")
        status, metrics = check_service_health(BaseTestService.logger, self.config_path)
        BaseTestService.logger.info(f"Health check metrics: {metrics}")
        assert status == True, f"Health check failed with metrics: {metrics}"
            
    def test_02_shutdown_logs(self):
        """Test service shutdown logs"""
        try:
            BaseTestService.logger.info("Testing shutdown logs...")
            # First stop the service
            BaseTestService.stop_service()
            # Wait for containers to stop
            time.sleep(CONTAINER_STOP_WAIT)
            # Verify containers are stopped
            client = docker.from_env()
            
            service_config = get_service_config(self.config_path)
            container_name = service_config["container_name"]
            
            containers = client.containers.list(filters={"name": container_name})
            assert len(containers) == 0, f"Containers with name {container_name} are still running"
            # Now check the logs
            assert check_shutdown_logs(BaseTestService.logger) == True, "Shutdown logs check failed"
        finally:
            if BaseTestService._setup_complete:
                BaseTestService.teardown_class()
                BaseTestService._setup_complete = False



if __name__ == "__main__":
    pytest.main(["-v", __file__, "-s", "--log-cli-level=INFO"])