# ChronosMatch Developer Guide & Standards
**Engineering Guidelines for High-Frequency Low-Latency Systems**

---

## 🛠️ Toolchain & Environment Setup

### 1. Requirements
* **Operating System**: Windows 10/11 x64 or Linux (Ubuntu 22.04+)
* **Python**: 3.11 to 3.15 (64-bit)
* **C/C++ Compiler**:
  * Windows: Microsoft Visual C++ Build Tools 2022/2026 (MSVC `cl.exe` v143+)
  * Linux: GCC 11+ or Clang 14+ with `-O3`
* **Python Libraries**: `cython`, `setuptools`, `pytest`, `rich`

### 2. Building the C/Cython Extensions
To compile `.pyx` modules into native `.pyd` (Windows) or `.so` (Linux) shared libraries:
```powershell
# Build in-place
python setup.py build_ext --inplace

# Clean build artifacts
Remove-Item -Recurse -Force build, *.pyd, chronos_match/*.pyd -ErrorAction SilentlyContinue
```

### 3. Running Unit & Integration Tests
```powershell
# Run the complete test suite
$env:PYTHONPATH="."
pytest -v -s tests/
```

---

## 📐 Low-Latency Engineering Coding Standards

To ensure ChronosMatch maintains its **sub-50 microsecond** execution target, all contributions must adhere to the following strict engineering rules:

### Rule 1: No Dynamic Memory Allocation (`malloc`/`new`) in the Hot Path
* Calling `malloc()`, `calloc()`, or creating Python objects inside the matching loop is strictly prohibited.
* All order nodes, book levels, and ring buffer slots must be drawn from **pre-allocated memory arenas**.
* Use static buffer pools and intrusive pointers.

### Rule 2: Floating-Point Arithmetic is Strictly Prohibited
* Never use `float` or `double` for price representations or arithmetic.
* Always use `int64_t` fixed-point pricing scaled by `PRICE_SCALE = 10,000` ($100.25 \rightarrow 1,002,500$).
* Spreads, fills, and price matching comparisons must be pure integer operations.

### Rule 3: Always Run Matching Under `nogil`
* Every Cython function on the critical path must be declared `cdef ... nogil:` or wrapped in `with nogil:`.
* Cython functions must accept and return C-types or C-pointers, not `PyObject*`.
* Verify with the Cython annotation tool:
  ```powershell
  cython -a chronos_match/engine/order_book_cy.pyx
  ```
  Ensure **no yellow lines** (Python C-API calls) exist inside the inner loops.

### Rule 4: Preserve Cache-Line Alignment (64 Bytes)
* Structs placed in shared memory must be explicitly aligned and padded to multiples of 64 bytes (`#pragma pack` or `__declspec(align(64))`).
* Producer write cursors and Consumer read cursors must reside on **separate 64-byte cache lines** to avoid false sharing.

### Rule 5: Zero-Copy Serialization
* Never serialize data using JSON, Pickle, or Protocol Buffers.
* Pack data directly into byte buffers via `struct.pack_into` or direct C pointer assignment.

---

## 🧪 Benchmarking Checklist

Before opening any pull request or marking a milestone as complete:
1. Verify all unit tests pass with `pytest`.
2. Run the IPC multi-process benchmark (`test_ring_buffer.py`) and confirm throughput exceeds $250,000\text{ orders/sec}$.
3. Verify zero buffer leaks or orphaned memory-mapped files.
