# Frame boundary (Present / SwapBuffers / vkQueuePresentKHR)

**Short:** The graphics API call that ends one rendered frame and starts the next.

**Details:**

- Each major graphics API has its own present-class call: ``SwapBuffers`` and the egl/glX variants in OpenGL, ``IDXGISwapChain::Present`` in Direct3D 11 and 12, and ``vkQueuePresentKHR`` in Vulkan.
- The call signals that the application is done drawing into the current back buffer and wants it shown.
- Internally it hands the image to the swap chain and the windowing or display system, which schedules the actual scanout.
- Time between two consecutive present timestamps (API present-return to present-return, or DxgKrnl ETW Present point timestamps when no API rows exist) on the same thread is the standard definition of a CPU frame duration.
- Present is a synchronization point: the driver may block here when the swap chain is full or when waiting for the next vertical blank under VSync.
- Repeating long and short Present intervals can indicate unstable CPU-GPU frame pipelining, but the cadence must be combined with thread-wait evidence before attributing a render queue flip-flop.
- Because every supported API exposes such a call, it is the most portable place to mark frame boundaries for profiling, FPS measurement, and overlays.

**See also:**

- [Graphics frame](graphics-frame.md)
- [FPS and frame time](fps-frame-time.md)
- [Swap chain](swap-chain.md)
- [CPU-GPU frame pipelining](cpu-gpu-frame-pipelining.md)
- [DXGI](dxgi.md)
