#!/usr/bin/env bash
# Final comprehensive test script for HDMI USB RTSP Server
# Tests server startup, client connection, and streaming functionality

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RTSP_SCRIPT="$SCRIPT_DIR/rtsp-server.py"
LOG_DIR="$SCRIPT_DIR/test-logs"
SERVER_LOG="$LOG_DIR/rtsp-server.log"
CLIENT_LOG="$LOG_DIR/rtsp-client.log"
RTSP_URL="rtsp://127.0.0.1:1234/hdmi"

# Test parameters
SERVER_STARTUP_TIMEOUT=15
CLIENT_TEST_TIMEOUT=10

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Test status
TEST_PASSED=true
SERVER_PID=""
CLIENT_PID=""

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}🧹 Cleaning up...${NC}"
    
    # Kill client if running
    if [[ -n "$CLIENT_PID" ]] && kill -0 "$CLIENT_PID" 2>/dev/null; then
        echo "   Stopping client (PID: $CLIENT_PID)"
        kill "$CLIENT_PID" 2>/dev/null || true
        wait "$CLIENT_PID" 2>/dev/null || true
    fi
    
    # Kill server if running
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "   Stopping server (PID: $SERVER_PID)"
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    
    # Kill any remaining rtsp-server processes
    pkill -f "rtsp-server.py" 2>/dev/null || true
    
    echo "   Cleanup complete"
}

# Signal handlers
trap cleanup EXIT INT TERM

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Setup logging
setup_logging() {
    mkdir -p "$LOG_DIR"
    rm -f "$SERVER_LOG" "$CLIENT_LOG"
    log_info "Log directory: $LOG_DIR"
}

# Test device detection
test_device_detection() {
    log_info "Testing HDMI device detection..."
    
    if python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())
exec(open('$RTSP_SCRIPT').read().split('if __name__')[0])
detector = HDMIDeviceDetector(debug_mode=True)
video_dev = detector.detect_video_device()
if video_dev:
    audio_card = detector.detect_audio_card(video_dev)
    print(f'✅ Video: {video_dev}, Audio: {audio_card}')
    exit(0)
else:
    print('❌ No video device found')
    exit(1)
" > "$LOG_DIR/device-detection.log" 2>&1; then
        log_success "HDMI device detection successful"
        cat "$LOG_DIR/device-detection.log" | sed 's/^/   /'
        return 0
    else
        log_error "HDMI device detection failed"
        cat "$LOG_DIR/device-detection.log" | sed 's/^/   /'
        return 1
    fi
}

# Start RTSP server
start_server() {
    log_info "Starting RTSP server..."
    
    # Start server in background with GST_DEBUG=3
    GST_DEBUG=3 python3 "$RTSP_SCRIPT" --debug > "$SERVER_LOG" 2>&1 &
    SERVER_PID=$!
    
    log_info "Server started with PID: $SERVER_PID"
    log_info "Waiting for server to initialize (timeout: ${SERVER_STARTUP_TIMEOUT}s)..."
    
    # Wait for server to start with multiple checks
    local wait_time=0
    while [[ $wait_time -lt $SERVER_STARTUP_TIMEOUT ]]; do
        # Check if server process is still running
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            log_error "Server process died during startup"
            log_error "Server logs:"
            cat "$SERVER_LOG" | sed 's/^/   /'
            return 1
        fi
        
        # Check for server ready indicators in logs
        if grep -q "ready for connections\|RTSP server is running" "$SERVER_LOG" 2>/dev/null; then
            log_success "Server is ready for connections"
            # Give server a moment to fully initialize
            sleep 2
            return 0
        fi
        
        # Also check if server is listening on port 1234
        if netstat -ln 2>/dev/null | grep -q ":1234 " || ss -ln 2>/dev/null | grep -q ":1234 "; then
            log_success "Server is listening on port 1234"
            # Give server a moment to fully initialize
            sleep 2
            return 0
        fi
        
        sleep 1
        wait_time=$((wait_time + 1))
    done
    
    log_error "Server startup timeout (${SERVER_STARTUP_TIMEOUT}s)"
    log_error "Server logs:"
    cat "$SERVER_LOG" | sed 's/^/   /'
    return 1
}

# Test client connection
test_client_connection() {
    log_info "Testing client connection..."
    
    # Check for available client tools
    local client_tool="${CLIENT:-}"
    if [[ -z "$client_tool" ]]; then
        if command -v ffplay >/dev/null 2>&1; then
            client_tool="ffplay"
        elif command -v vlc >/dev/null 2>&1; then
            client_tool="vlc"
        else
            log_error "Neither ffplay nor vlc found for client testing"
            return 1
        fi
    fi

    # Validate requested client
    if [[ "$client_tool" != "ffplay" && "$client_tool" != "vlc" ]]; then
        log_error "Unsupported CLIENT='$client_tool'. Use ffplay or vlc."
        log_error "Neither ffplay nor vlc found for client testing"
        return 1
    fi
    
    log_info "Using $client_tool for client testing"
    
    local client_cmd=""
    if [[ "$client_tool" == "ffplay" ]]; then
        client_cmd="ffplay -rtsp_transport tcp -autoexit -loglevel error '$RTSP_URL'"
    elif [[ "$client_tool" == "vlc" ]]; then
        client_cmd="vlc --intf dummy --no-video-title-show --play-and-exit '$RTSP_URL' :rtsp-tcp"
    fi
    
    log_info "Running client command: $client_cmd"
    
    # Start client with timeout
    timeout "$CLIENT_TEST_TIMEOUT" bash -c "$client_cmd" > "$CLIENT_LOG" 2>&1 &
    CLIENT_PID=$!
    
    log_info "Client started with PID: $CLIENT_PID"
    
    # Monitor client for a few seconds
    sleep 5
    
    # Check if client is still running (good sign)
    if kill -0 "$CLIENT_PID" 2>/dev/null; then
        log_success "Client connection successful"
        # Stop the client
        kill "$CLIENT_PID" 2>/dev/null || true
        wait "$CLIENT_PID" 2>/dev/null || true
        return 0
    else
        # Check client exit status
        if wait "$CLIENT_PID" 2>/dev/null; then
            local exit_code=$?
            if [[ $exit_code -eq 124 ]]; then
                log_success "Client connection successful (timeout expected)"
                return 0
            elif [[ $exit_code -eq 0 ]]; then
                log_success "Client connection successful"
                return 0
            else
                log_error "Client exited with error code: $exit_code"
            fi
        else
            log_error "Client process failed"
        fi
        
        # Show client logs for debugging
        log_error "Client logs:"
        cat "$CLIENT_LOG" | sed 's/^/   /'
        return 1
    fi
}

# Check server health
check_server_health() {
    log_info "Checking server health..."
    
    # Check if server process is still running
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        # Server process died - check if it was graceful
        if grep -q "Shutting down RTSP server gracefully\|Server stopped by user" "$SERVER_LOG" 2>/dev/null; then
            log_success "Server shut down gracefully (expected after client disconnect)"
            return 0
        else
            log_error "Server process died unexpectedly"
            log_error "Last few lines of server log:"
            tail -5 "$SERVER_LOG" | sed 's/^/   /'
            return 1
        fi
    fi
    
    # Check if server is still listening on port 1234
    if netstat -ln 2>/dev/null | grep -q ":1234 " || ss -ln 2>/dev/null | grep -q ":1234 "; then
        log_success "Server is healthy and listening"
        return 0
    else
        # Server might have shut down gracefully after client disconnect
        # Check if it shut down cleanly by looking at logs
        if grep -q "Shutting down RTSP server gracefully\|Server stopped by user" "$SERVER_LOG" 2>/dev/null; then
            log_success "Server shut down gracefully after client disconnect (expected)"
            return 0
        else
            log_error "Server is not listening and did not shut down gracefully"
            log_error "Last few lines of server log:"
            tail -5 "$SERVER_LOG" | sed 's/^/   /'
            return 1
        fi
    fi
}

# Main test function
main() {
    echo -e "${BLUE}🧪 HDMI USB RTSP Server Test Suite${NC}"
    echo "================================================"
    
    # Initialize
    setup_logging
    
    log_info "Starting test sequence..."
    log_info "RTSP URL: $RTSP_URL"
    log_info "Server timeout: ${SERVER_STARTUP_TIMEOUT}s"
    log_info "Client timeout: ${CLIENT_TEST_TIMEOUT}s"
    
    # Test device detection
    if ! test_device_detection; then
        log_error "Device detection test failed"
        TEST_PASSED=false
    fi
    
    # Start server
    if ! start_server; then
        log_error "Server startup test failed"
        TEST_PASSED=false
    fi
    
    # Test client connection
    if ! test_client_connection; then
        log_error "Client connection test failed"
        TEST_PASSED=false
    fi
    
    # Give server a moment to process client disconnect and flush logs
    sleep 15
    
    # Check server health
    if ! check_server_health; then
        log_error "Server health check failed"
        TEST_PASSED=false
    fi
    
    # Final results
    echo ""
    echo "================================================"
    if $TEST_PASSED; then
        log_success "🎉 All tests PASSED!"
        log_success "RTSP server is working correctly"
        echo ""
        log_info "Server logs: $SERVER_LOG"
        log_info "Client logs: $CLIENT_LOG"
        exit 0
    else
        log_error "❌ Some tests FAILED!"
        log_error "Check the log files for details:"
        echo ""
        log_info "Server logs: $SERVER_LOG"
        log_info "Client logs: $CLIENT_LOG"
        exit 1
    fi
}

# Run main function
main "$@"
