# CPU-GPU frame pipelining




**Short:** CPU-GPU frame pipelining lets the CPU prepare a newer frame while the GPU renders an earlier one; losing that stable overlap can produce repeating waits and uneven Present cadence.

**Details:**

- Flip-model swap chains queue presents. When the present queue reaches its configured limit, the application must wait for an earlier frame to leave the queue before it can submit another.
- A stable pipeline overlaps CPU submission for one frame with GPU execution of another. Reducing the number of frames allowed in flight reduces latency but also reduces this CPU-GPU parallelism.
- **Render queue flip-flop**, also called **CPU-GPU swap-chain desynchronization** here, is a repeating long-short CPU present pattern: the CPU catches the GPU or a full present queue and waits on one frame, then submits the next frame quickly after queue capacity becomes available.
- Detect the pattern from `frame_time_pattern.oscillation_detected` (or equivalent cadence analysis) plus a frame-local fence, Present, or swap-chain wait. Cadence alone does not identify the wait target or prove that the pattern reached the display.
- **Serialized frame production** is the separate state where the overlap never exists: the application waits for the GPU to finish the frame it just submitted, so CPU submission and GPU execution strictly alternate. It produces the same long-short present cadence as a flip-flop, because a blocking submit path releases its queued presents in a burst and then stalls, which is why cadence cannot distinguish the two. The distinguishing evidence is whether the wait recurs in every iteration or only on the long half.
- Distinguish this pattern from DLSS Frame Generation present bunching, display-side flip scheduling misses, and the Reflex **OS Render Queue** latency stage.

**Authoritative background:**

- [Microsoft: Reduce latency with DXGI 1.3 swap chains](https://learn.microsoft.com/en-us/windows/uwp/gaming/reduce-latency-with-dxgi-1-3-swap-chains) explains queued flips, Present blocking when the queue has no room, and the CPU-GPU parallelism tradeoff of frame-latency settings.
- [Microsoft: GetFrameLatencyWaitableObject](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_3/nf-dxgi1_3-idxgiswapchain2-getframelatencywaitableobject) documents synchronizing each new frame with completion of an earlier presentation.
- [Intel: D3D12 flip-model swap-chain sample](https://www.intel.com/content/www/us/en/developer/articles/code-sample/sample-application-for-direct3d-12-flip-model-swap-chains.html) describes the present-queue limit, Present blocking, and the need for multiple frames in flight to preserve CPU-GPU overlap.

**See also:**

- [Swap chain](swap-chain.md)
- [Frame boundary / Present](frame-boundary-present.md)
- [Fence](fence.md)
- [Graphics frame](graphics-frame.md)
- [Stutter](stutter.md)
