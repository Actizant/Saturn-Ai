---
name: performance-profiler
description: CPU & memory profiling, latency bottleneck identification, database query optimization, and memory leak analysis.
triggers:
  - "profile performance"
  - "find memory leak"
  - "optimize slow code"
  - "cpu benchmark"
---

# Performance Profiler & System Optimizer Skill

## Profiling Protocol
1. **Identification of Bottlenecks**:
   - Python: Run `cProfile`, `timeit`, or inspect with `tracemalloc`.
   - Node: Inspect Event Loop lag, heap snapshots, and V8 profiler outputs.
2. **Algorithmic & Data Structure Optimization**:
   - Spot \(O(N^2)\) or \(O(N^3)\) nested loops that can be optimized to \(O(N)\) using hash maps/sets.
   - Replace unbounded in-memory list aggregations with generators and streaming pipelines.
3. **Database Latency**:
   - Eliminate N+1 query patterns by using eager loading / joins (`joinedload`, `selectinload`, `include`).
   - Add missing indexes on foreign keys and frequently filtered columns.
4. **I/O & Concurrency**:
   - Offload synchronous blocking disk/network I/O out of the main async event loop into worker threads (`asyncio.to_thread` or thread pools).
