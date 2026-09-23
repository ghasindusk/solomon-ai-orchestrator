import subprocess
import sys
import pathlib
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.telemetry import get_gpu_telemetry


def test_returns_none_when_nvidia_smi_not_on_path():
    with patch("shutil.which", return_value=None):
        assert get_gpu_telemetry() is None


def test_parses_valid_csv_output():
    stdout = "NVIDIA GeForce RTX 4070 Ti, 12, 4096, 12288\n"
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
            result = get_gpu_telemetry()
    assert result == {
        "name": "NVIDIA GeForce RTX 4070 Ti",
        "gpu_load_percent": 12.0,
        "vram_used_mb": 4096.0,
        "vram_total_mb": 12288.0,
    }


def test_returns_none_on_nonzero_returncode():
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
            assert get_gpu_telemetry() is None


def test_returns_none_on_malformed_output():
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="not,enough,fields", stderr=""
            )
            assert get_gpu_telemetry() is None


def test_returns_none_when_subprocess_raises():
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
        with patch("subprocess.run", side_effect=OSError("boom")):
            assert get_gpu_telemetry() is None
