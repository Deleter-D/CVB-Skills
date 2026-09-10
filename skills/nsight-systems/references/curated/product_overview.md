# Nsight Systems product overview

> Curated overlay: release-reviewed synthesis. Source inputs: QuadD/Docs/Rst/index.rst; QuadD/Docs/Rst/UserGuide; Installed nsys --version recorded in indexes/source_manifest.json at build time. Official generated docs, live CLI/recipe help, and report-tool evidence remain authoritative when facts differ.




NVIDIA Nsight Systems is a system-wide performance analysis tool. It combines statistical CPU sampling with timeline tracing so users can correlate CPU work, GPU work, OS runtime activity, CUDA API calls, graphics APIs, NVTX ranges, MPI, NCCL, and other events depending on platform and trace settings.

Use it to find bottlenecks across the whole application, such as launch overhead, CPU-GPU serialization, GPU gaps, pipeline bubbles, thread stalls, I/O delays, and multi-process or multi-rank imbalance.

Nsight Systems is different from the deeper-dive Nsight tools. Nsight Systems explains where time goes across the application timeline. Nsight Compute provides deeper per-kernel analysis after a compute kernel has been identified. Nsight Graphics provides deeper per-frame and per-draw analysis after a slow frame or expensive graphics workload has been identified.

## Nsight tool boundaries

Use Nsight Systems evidence for timeline-level questions: which phases are expensive, which kernels or CUDA APIs dominate time, where CPU/GPU gaps appear, how NVTX/MPI/NCCL activity lines up, and which kernels are good candidates for deeper inspection.

Use Nsight Compute for kernel-level counter questions such as Speed Of Light sections, occupancy, register pressure, SASS/source correlation, roofline charts, replay modes, warp/PM sampling, or metric names such as `gpc__cycles_elapsed.max`. The Nsight Systems skill can identify candidate kernels and handoff metadata from an `.nsys-rep`, but it should not explain detailed Nsight Compute metric semantics or claim Nsight Compute counter values unless an Nsight Compute reference/tool has provided that evidence.

Nsight Graphics is a separate tool for frame-level debugging and profiling of graphics applications, covering Direct3D 11/12, Vulkan, OpenGL, and OptiX, including DXR and Vulkan Ray Tracing. It captures a frame and lets users step through draw calls and inspect API and pipeline state, shaders, and resources such as textures, buffers, and render targets. Its GPU Trace activity reports hardware unit throughput and utilization across a frame, its Shader Profiler attributes cost to individual shaders, and its acceleration-structure viewer inspects ray-tracing geometry.

Use Nsight Systems evidence for timeline-level graphics questions: which frames are slow, present cadence and frame pacing, CPU-GPU serialization in the render loop, GPU gaps between frames, and which graphics API calls or GPU ranges dominate a frame (for example via `report-fact --intent graphics_api_summary`, optionally scoped with `--frame <N>`). Use Nsight Graphics for pipeline-level questions such as per-draw-call cost, shader hotspots, API/pipeline state, render-target and resource inspection, or GPU unit utilization within a captured frame. The Nsight Systems skill can identify slow frames and expensive graphics API activity from an `.nsys-rep`, but it should not explain Nsight Graphics frame-debugger state, shader-profiler results, or GPU Trace unit metrics unless an Nsight Graphics reference/tool has provided that evidence.
