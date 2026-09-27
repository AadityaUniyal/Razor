import subprocess
import os
import sys
import pytest

def test_frontend_app_js_syntax():
    """Verify syntax validity of frontend/app.js using node -c"""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_js_path = os.path.join(root_dir, "frontend", "app.js")
    result = subprocess.run(["node", "-c", app_js_path], capture_output=True, text=True)
    assert result.returncode == 0, f"Syntax error in app.js:\n{result.stderr}"

def test_frontend_empirical_suite():
    """Execute tests/test_frontend_empirical.js verifying displayValue, renderStructuredObject, factor checklist, and dynamic limits"""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    test_js_path = os.path.join(root_dir, "tests", "test_frontend_empirical.js")
    result = subprocess.run(["node", test_js_path], capture_output=True, text=True, cwd=root_dir)
    print("STDOUT:\n", result.stdout)
    print("STDERR:\n", result.stderr)
    assert result.returncode == 0, f"Empirical tests failed:\n{result.stderr}\n{result.stdout}"
