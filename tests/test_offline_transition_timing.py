"""Test online to offline transition timing for Device Online Tracker."""
import pytest
import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock

from custom_components.DeviceOnlineTracker import (
    check_device_status,
    update_device_data,
    MODE_PARALLEL,
    MODE_RETRY,
    DEFAULT_OFFLINE_THRESHOLD,
    DEFAULT_PING_COUNT,
    DEFAULT_RETRY_INTERVAL,
    DEFAULT_RETRY_PING_COUNT,
)


class TestOnlineToOfflineTransition:
    """Test cases for measuring online to offline transition time."""

    @pytest.fixture
    def device_data_online(self):
        """Create device data in online state."""
        return {
            "name": "test_device",
            "online_time": 100,
            "last_check": datetime.now(),
            "last_date": datetime.now().date(),
            "is_online": True,
            "fail_count": 0
        }

    @pytest.fixture
    def mock_store(self):
        """Mock storage."""
        store = MagicMock()
        store.async_load = AsyncMock(return_value={})
        store.async_save = AsyncMock()
        return store

    def create_ping_mock(self, results_sequence):
        """Create a mock that returns different results on each call.
        
        Args:
            results_sequence: List of packets_received values for each call
        """
        call_idx = [0]
        
        async def mock_ping(*args, **kwargs):
            result = MagicMock()
            if call_idx[0] < len(results_sequence):
                result.packets_received = results_sequence[call_idx[0]]
            else:
                result.packets_received = 0
            result.packets_sent = kwargs.get('count', 3)
            call_idx[0] += 1
            return result
        
        return mock_ping, call_idx

    @pytest.mark.asyncio
    async def test_parallel_mode_single_cycle_timing(self, device_data_online, mock_store):
        """Test single detection cycle timing in parallel mode.
        
        Parallel mode: sends ping_count packets in one call.
        Expected time: ~1-2 seconds (timeout=2, interval=0.5)
        """
        # All packets fail (offline)
        mock_ping, _ = self.create_ping_mock([0])  # 0 packets received
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping):
            start_time = time.perf_counter()
            
            result = await update_device_data(
                device_data_online,
                "192.168.1.100",
                mock_store,
                "test_entry_id",
                offline_threshold=3,
                ping_count=3,
                mode=MODE_PARALLEL
            )
            
            elapsed = time.perf_counter() - start_time
            
            print(f"\n[Parallel Mode - Single Cycle]")
            print(f"  Elapsed time: {elapsed:.3f}s")
            print(f"  Device status: {'online' if result['is_online'] else 'offline'}")
            print(f"  Fail count: {result['fail_count']}/3")
            
            # After 1 failed detection, device should still be online (threshold=3)
            assert result["is_online"] is True
            assert result["fail_count"] == 1

    @pytest.mark.asyncio
    async def test_parallel_mode_full_offline_timing(self, device_data_online, mock_store):
        """Test time to reach offline state in parallel mode.
        
        Needs 3 consecutive failed cycles to go offline.
        This simulates 3 separate detection cycles.
        """
        # All detections fail
        mock_ping, call_count = self.create_ping_mock([0, 0, 0])
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping):
            start_time = time.perf_counter()
            
            # Simulate 3 detection cycles
            for cycle in range(3):
                result = await update_device_data(
                    device_data_online,
                    "192.168.1.100",
                    mock_store,
                    "test_entry_id",
                    offline_threshold=3,
                    ping_count=3,
                    mode=MODE_PARALLEL
                )
                cycle_time = time.perf_counter() - start_time
                print(f"\n[Parallel Mode - Cycle {cycle + 1}]")
                print(f"  Cumulative time: {cycle_time:.3f}s")
                print(f"  Device status: {'online' if result['is_online'] else 'offline'}")
                print(f"  Fail count: {result['fail_count']}/3")
            
            total_elapsed = time.perf_counter() - start_time
            
            print(f"\n[Parallel Mode - Summary]")
            print(f"  Total time to offline: {total_elapsed:.3f}s")
            print(f"  Ping calls made: {call_count[0]}")
            
            # After 3 failed cycles, device should be offline
            assert result["is_online"] is False
            assert result["fail_count"] >= 3

    @pytest.mark.asyncio
    async def test_retry_mode_immediate_offline_timing(self, device_data_online, mock_store):
        """Test time to reach offline state in retry mode.
        
        Retry mode: immediately retries when offline detected.
        Expected: First ping fails → retry 2 more times → offline
        With retry_interval=5s, total ~10-15s
        """
        # All retries fail
        mock_ping, call_count = self.create_ping_mock([0, 0, 0])
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping):
            start_time = time.perf_counter()
            
            result = await update_device_data(
                device_data_online,
                "192.168.1.100",
                mock_store,
                "test_entry_id",
                offline_threshold=3,
                ping_count=3,
                retry_interval=0.1,  # Use short interval for testing
                retry_ping_count=1,
                mode=MODE_RETRY
            )
            
            elapsed = time.perf_counter() - start_time
            
            print(f"\n[Retry Mode - Immediate Offline Detection]")
            print(f"  Total time: {elapsed:.3f}s")
            print(f"  Device status: {'online' if result['is_online'] else 'offline'}")
            print(f"  Fail count: {result['fail_count']}")
            print(f"  Ping calls made: {call_count[0]}")
            
            # Device should be offline after retries
            assert result["is_online"] is False
            assert call_count[0] == 3  # Initial + 2 retries

    @pytest.mark.asyncio
    async def test_retry_mode_recovery_during_retry(self, device_data_online, mock_store):
        """Test device recovery during retry sequence.
        
        Scenario: First ping fails, second retry succeeds.
        """
        # First fails, second succeeds
        mock_ping, call_count = self.create_ping_mock([0, 1])
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping):
            start_time = time.perf_counter()
            
            result = await update_device_data(
                device_data_online,
                "192.168.1.100",
                mock_store,
                "test_entry_id",
                offline_threshold=3,
                ping_count=3,
                retry_interval=0.1,
                retry_ping_count=1,
                mode=MODE_RETRY
            )
            
            elapsed = time.perf_counter() - start_time
            
            print(f"\n[Retry Mode - Recovery During Retry]")
            print(f"  Total time: {elapsed:.3f}s")
            print(f"  Device status: {'online' if result['is_online'] else 'offline'}")
            print(f"  Fail count: {result['fail_count']}")
            print(f"  Ping calls made: {call_count[0]}")
            
            # Device should remain online after recovery
            assert result["is_online"] is True
            assert result["fail_count"] == 0

    @pytest.mark.asyncio
    async def test_realistic_timing_comparison(self, mock_store):
        """Compare realistic timing between parallel and retry modes.
        
        Uses actual retry_interval to measure real-world timing.
        """
        print("\n" + "=" * 60)
        print("REALISTIC TIMING COMPARISON")
        print("=" * 60)
        
        # Test with realistic settings (but shorter for testing)
        retry_interval = 1.0  # 1 second instead of 5
        
        # --- Parallel Mode ---
        device_data_parallel = {
            "name": "test_device",
            "online_time": 100,
            "last_check": datetime.now(),
            "last_date": datetime.now().date(),
            "is_online": True,
            "fail_count": 0
        }
        
        mock_ping_p, call_count_p = self.create_ping_mock([0, 0, 0])
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping_p):
            start_p = time.perf_counter()
            
            for _ in range(3):
                result_p = await update_device_data(
                    device_data_parallel,
                    "192.168.1.100",
                    mock_store,
                    "test_entry_id",
                    offline_threshold=3,
                    ping_count=3,
                    mode=MODE_PARALLEL
                )
            
            elapsed_p = time.perf_counter() - start_p
        
        # --- Retry Mode ---
        device_data_retry = {
            "name": "test_device",
            "online_time": 100,
            "last_check": datetime.now(),
            "last_date": datetime.now().date(),
            "is_online": True,
            "fail_count": 0
        }
        
        mock_ping_r, call_count_r = self.create_ping_mock([0, 0, 0])
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping_r):
            start_r = time.perf_counter()
            
            result_r = await update_device_data(
                device_data_retry,
                "192.168.1.100",
                mock_store,
                "test_entry_id",
                offline_threshold=3,
                ping_count=3,
                retry_interval=retry_interval,
                retry_ping_count=1,
                mode=MODE_RETRY
            )
            
            elapsed_r = time.perf_counter() - start_r
        
        print(f"\n[Parallel Mode]")
        print(f"  Time for 3 cycles: {elapsed_p:.3f}s")
        print(f"  (Real-world: 3 × scan_interval, e.g., 3 × 60s = 180s)")
        print(f"  Ping calls: {call_count_p[0]}")
        print(f"  Final status: {'online' if result_p['is_online'] else 'offline'}")
        
        print(f"\n[Retry Mode]")
        print(f"  Time for immediate detection: {elapsed_r:.3f}s")
        print(f"  (Real-world with retry_interval=5s: ~10-15s)")
        print(f"  Ping calls: {call_count_r[0]}")
        print(f"  Final status: {'online' if result_r['is_online'] else 'offline'}")
        
        print(f"\n[Comparison]")
        print(f"  Retry mode is faster for immediate detection")
        print(f"  But parallel mode is better for scheduled scans")


class TestTimingWithRealDelays:
    """Test with actual sleep delays to measure real timing."""

    @pytest.fixture
    def device_data_online(self):
        """Create device data in online state."""
        return {
            "name": "test_device",
            "online_time": 100,
            "last_check": datetime.now(),
            "last_date": datetime.now().date(),
            "is_online": True,
            "fail_count": 0
        }

    @pytest.fixture
    def mock_store(self):
        """Mock storage."""
        store = MagicMock()
        store.async_load = AsyncMock(return_value={})
        store.async_save = AsyncMock()
        return store

    @pytest.mark.asyncio
    async def test_retry_mode_with_real_delays(self, device_data_online, mock_store):
        """Test retry mode with actual sleep delays.
        
        This test uses short but real delays to verify timing behavior.
        """
        retry_interval = 0.5  # 500ms for testing
        
        async def mock_ping_offline(*args, **kwargs):
            result = MagicMock()
            result.packets_received = 0
            result.packets_sent = kwargs.get('count', 3)
            await asyncio.sleep(0.1)  # Simulate ping latency
            return result
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping_offline):
            start_time = time.perf_counter()
            
            result = await update_device_data(
                device_data_online,
                "192.168.1.100",
                mock_store,
                "test_entry_id",
                offline_threshold=3,
                ping_count=3,
                retry_interval=retry_interval,
                retry_ping_count=1,
                mode=MODE_RETRY
            )
            
            elapsed = time.perf_counter() - start_time
            
            # Expected: 
            # - Initial ping: ~0.1s
            # - Wait: 0.5s
            # - Retry 1: ~0.1s
            # - Wait: 0.5s
            # - Retry 2: ~0.1s
            # Total: ~1.3s
            
            print(f"\n[Retry Mode - Real Delays]")
            print(f"  retry_interval: {retry_interval}s")
            print(f"  offline_threshold: 3")
            print(f"  Expected time: ~{retry_interval * 2 + 0.3:.1f}s")
            print(f"  Actual time: {elapsed:.3f}s")
            print(f"  Device status: {'online' if result['is_online'] else 'offline'}")
            
            assert result["is_online"] is False
            # Allow some tolerance for timing
            expected_min = retry_interval * 2  # At least 2 retry intervals
            assert elapsed >= expected_min, f"Expected at least {expected_min}s, got {elapsed:.3f}s"


class TestConcurrentPing:
    """Test concurrent ping functionality."""

    @pytest.fixture
    def mock_store(self):
        """Mock storage."""
        store = MagicMock()
        store.async_load = AsyncMock(return_value={})
        store.async_save = AsyncMock()
        return store

    def create_device_data(self, name: str, is_online: bool = True):
        """Create device data for testing."""
        return {
            "name": name,
            "online_time": 100 if is_online else 0,
            "last_check": datetime.now(),
            "last_date": datetime.now().date(),
            "is_online": is_online,
            "fail_count": 0
        }

    @pytest.mark.asyncio
    async def test_concurrent_vs_sequential_timing(self, mock_store):
        """Test that concurrent execution is faster than sequential.
        
        Simulates pinging multiple devices and compares timing.
        """
        print("\n" + "=" * 60)
        print("CONCURRENT VS SEQUENTIAL TIMING TEST")
        print("=" * 60)
        
        num_devices = 5
        ping_delay = 0.3  # Simulated ping latency per device
        
        # Create mock ping that takes some time
        async def mock_ping_with_delay(*args, **kwargs):
            await asyncio.sleep(ping_delay)
            result = MagicMock()
            result.packets_received = 3
            result.packets_sent = 3
            return result
        
        # --- Sequential execution ---
        devices_seq = [self.create_device_data(f"device_{i}") for i in range(num_devices)]
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping_with_delay):
            start_seq = time.perf_counter()
            
            for i, device_data in enumerate(devices_seq):
                await update_device_data(
                    device_data,
                    f"192.168.1.{100+i}",
                    mock_store,
                    f"entry_{i}",
                    offline_threshold=3,
                    ping_count=3,
                    mode=MODE_PARALLEL
                )
            
            elapsed_seq = time.perf_counter() - start_seq
        
        # --- Concurrent execution ---
        devices_conc = [self.create_device_data(f"device_{i}") for i in range(num_devices)]
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping_with_delay):
            start_conc = time.perf_counter()
            
            tasks = []
            for i, device_data in enumerate(devices_conc):
                task = update_device_data(
                    device_data,
                    f"192.168.1.{100+i}",
                    mock_store,
                    f"entry_{i}",
                    offline_threshold=3,
                    ping_count=3,
                    mode=MODE_PARALLEL
                )
                tasks.append(task)
            
            await asyncio.gather(*tasks)
            elapsed_conc = time.perf_counter() - start_conc
        
        print(f"\n[Sequential Execution]")
        print(f"  Devices: {num_devices}")
        print(f"  Ping delay per device: {ping_delay}s")
        print(f"  Total time: {elapsed_seq:.3f}s")
        print(f"  Expected: ~{num_devices * ping_delay:.1f}s")
        
        print(f"\n[Concurrent Execution]")
        print(f"  Devices: {num_devices}")
        print(f"  Total time: {elapsed_conc:.3f}s")
        print(f"  Expected: ~{ping_delay:.1f}s (all parallel)")
        
        print(f"\n[Speedup]")
        speedup = elapsed_seq / elapsed_conc
        print(f"  Concurrent is {speedup:.1f}x faster")
        
        # Concurrent should be significantly faster
        assert elapsed_conc < elapsed_seq, "Concurrent should be faster than sequential"
        assert speedup > 2.0, f"Expected at least 2x speedup, got {speedup:.1f}x"

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self, mock_store):
        """Test that semaphore properly limits concurrent executions.
        
        Verifies that max_concurrent setting is respected.
        """
        print("\n" + "=" * 60)
        print("SEMAPHORE CONCURRENCY LIMIT TEST")
        print("=" * 60)
        
        num_devices = 10
        max_concurrent = 3
        ping_delay = 0.2
        
        # Track concurrent executions
        current_concurrent = [0]
        max_observed_concurrent = [0]
        
        async def mock_ping_tracking(*args, **kwargs):
            current_concurrent[0] += 1
            max_observed_concurrent[0] = max(max_observed_concurrent[0], current_concurrent[0])
            await asyncio.sleep(ping_delay)
            current_concurrent[0] -= 1
            result = MagicMock()
            result.packets_received = 3
            result.packets_sent = 3
            return result
        
        devices = [self.create_device_data(f"device_{i}") for i in range(num_devices)]
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def limited_update(device_data, host, entry_id):
            async with semaphore:
                await update_device_data(
                    device_data,
                    host,
                    mock_store,
                    entry_id,
                    offline_threshold=3,
                    ping_count=3,
                    mode=MODE_PARALLEL
                )
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping_tracking):
            start = time.perf_counter()
            
            tasks = []
            for i, device_data in enumerate(devices):
                task = limited_update(device_data, f"192.168.1.{100+i}", f"entry_{i}")
                tasks.append(task)
            
            await asyncio.gather(*tasks)
            elapsed = time.perf_counter() - start
        
        print(f"\n[Semaphore Test]")
        print(f"  Total devices: {num_devices}")
        print(f"  Max concurrent limit: {max_concurrent}")
        print(f"  Max observed concurrent: {max_observed_concurrent[0]}")
        print(f"  Total time: {elapsed:.3f}s")
        
        # Expected time: ceil(num_devices / max_concurrent) * ping_delay
        import math
        expected_batches = math.ceil(num_devices / max_concurrent)
        expected_time = expected_batches * ping_delay
        print(f"  Expected time: ~{expected_time:.1f}s ({expected_batches} batches)")
        
        # Verify concurrency was limited
        assert max_observed_concurrent[0] <= max_concurrent, \
            f"Max concurrent {max_observed_concurrent[0]} exceeded limit {max_concurrent}"
        
        # Verify timing is reasonable (not too fast, not too slow)
        assert elapsed >= expected_time * 0.8, f"Finished too fast, semaphore may not be working"
        
        print(f"\n  ✓ Concurrency properly limited to {max_concurrent}")

    @pytest.mark.asyncio
    async def test_concurrent_mixed_results(self, mock_store):
        """Test concurrent execution with mixed online/offline results."""
        print("\n" + "=" * 60)
        print("CONCURRENT MIXED RESULTS TEST")
        print("=" * 60)
        
        num_devices = 6
        # Devices 0,2,4 will be online; 1,3,5 will be offline
        
        async def mock_ping_mixed(address, *args, **kwargs):
            await asyncio.sleep(0.1)
            result = MagicMock()
            # Extract device number from IP
            device_num = int(address.split('.')[-1]) - 100
            if device_num % 2 == 0:  # Even devices are online
                result.packets_received = 3
            else:  # Odd devices are offline
                result.packets_received = 0
            result.packets_sent = 3
            return result
        
        devices = [self.create_device_data(f"device_{i}") for i in range(num_devices)]
        
        with patch("custom_components.DeviceOnlineTracker.async_ping", side_effect=mock_ping_mixed):
            tasks = []
            for i, device_data in enumerate(devices):
                task = update_device_data(
                    device_data,
                    f"192.168.1.{100+i}",
                    mock_store,
                    f"entry_{i}",
                    offline_threshold=1,  # Immediate offline detection
                    ping_count=3,
                    mode=MODE_PARALLEL
                )
                tasks.append(task)
            
            await asyncio.gather(*tasks)
        
        print(f"\n[Results]")
        online_count = 0
        offline_count = 0
        for i, device_data in enumerate(devices):
            status = "online" if device_data["is_online"] else "offline"
            expected = "online" if i % 2 == 0 else "offline"
            match = "✓" if status == expected else "✗"
            print(f"  Device {i}: {status} (expected: {expected}) {match}")
            if device_data["is_online"]:
                online_count += 1
            else:
                offline_count += 1
        
        print(f"\n[Summary]")
        print(f"  Online: {online_count}")
        print(f"  Offline: {offline_count}")
        
        # Verify results
        assert online_count == 3, f"Expected 3 online, got {online_count}"
        assert offline_count == 3, f"Expected 3 offline, got {offline_count}"
        
        # Verify specific devices
        for i, device_data in enumerate(devices):
            expected_online = (i % 2 == 0)
            assert device_data["is_online"] == expected_online, \
                f"Device {i} should be {'online' if expected_online else 'offline'}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
