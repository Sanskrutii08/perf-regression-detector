import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * V2 variant - in-memory processing.
 *
 * Reads the entire file into a byte[] ONCE on the main thread, then has
 * worker threads process disjoint, line-aligned ranges of that SAME
 * in-memory array in parallel. No thread ever touches the disk after the
 * initial read - this isolates whether the disk-based V2 was bottlenecked
 * by concurrent file I/O contention rather than by the actual
 * parsing/aggregation work itself.
 *
 * The byte[] is only ever read, never written, by the worker threads, so
 * sharing it across threads is safe without any synchronization.
 *
 * Usage: java LogAggregatorV2InMemory <path-to-log-file> [num-threads]
 */
public class MultiThreadLogAggregator {

    public static void main(String[] args) throws Exception {
        String path = args.length > 0 ? args[0] : "sample.log";
        int numThreads = args.length > 1 ? Integer.parseInt(args[1]) : Runtime.getRuntime().availableProcessors();

        long overallStart = System.nanoTime();

        long t0 = System.nanoTime();
        byte[] data = Files.readAllBytes(Path.of(path));
        long t1 = System.nanoTime();

        int[] boundaries = computeChunkBoundaries(data, numThreads);
        long t2 = System.nanoTime();

        ExecutorService pool = Executors.newFixedThreadPool(numThreads);
        List<Future<Map<String, ServiceStats>>> futures = new ArrayList<>();

        for (int i = 0; i < numThreads; i++) {
            final int chunkStart = boundaries[i];
            final int chunkEnd = boundaries[i + 1];
            futures.add(pool.submit(() -> processChunk(data, chunkStart, chunkEnd)));
        }

        long t3 = System.nanoTime(); // all tasks submitted, workers running now

        Map<String, ServiceStats> merged = new HashMap<>();
        for (Future<Map<String, ServiceStats>> future : futures) {
            Map<String, ServiceStats> local = future.get();
            for (Map.Entry<String, ServiceStats> e : local.entrySet()) {
                merged.computeIfAbsent(e.getKey(), k -> new ServiceStats()).merge(e.getValue());
            }
        }
        long t4 = System.nanoTime(); // all workers finished + merged

        pool.shutdown();

        long totalLines = 0;
        for (ServiceStats s : merged.values()) totalLines += s.total;

        long overallElapsedMs = (System.nanoTime() - overallStart) / 1_000_000;

        System.out.printf("[timing] file read into memory: %d ms%n", (t1 - t0) / 1_000_000);
        System.out.printf("[timing] boundary computation: %d ms%n", (t2 - t1) / 1_000_000);
        System.out.printf("[timing] pool creation + submit: %d ms%n", (t3 - t2) / 1_000_000);
        System.out.printf("[timing] worker processing + merge: %d ms%n", (t4 - t3) / 1_000_000);
        System.out.println();

        ReportPrinter.print(merged, totalLines, overallElapsedMs, "V2-in-memory (" + numThreads + " threads)", (ThreadPoolExecutor)(pool));
    }

    /** Same idea as the disk version's boundary finder, but operating on the in-memory array. */
    private static int[] computeChunkBoundaries(byte[] data, int numChunks) {
        int[] boundaries = new int[numChunks + 1];
        boundaries[0] = 0;
        boundaries[numChunks] = data.length;

        for (int i = 1; i < numChunks; i++) {
            int approx = (int) (((long) data.length * i) / numChunks);
            int pos = approx;
            while (pos < data.length && data[pos] != '\n') {
                pos++;
            }
            boundaries[i] = Math.min(pos + 1, data.length);
        }
        return boundaries;
    }

    /** Scans this thread's byte range directly, with zero disk access - the array is already in RAM. */
    private static Map<String, ServiceStats> processChunk(byte[] data, int start, int end) {
        Map<String, ServiceStats> localStats = new HashMap<>();

        int lineStart = start;
        for (int i = start; i < end; i++) {
            if (data[i] == '\n') {
                if (i > lineStart) {
                    String line = new String(data, lineStart, i - lineStart, StandardCharsets.UTF_8);
                    processLine(line, localStats);
                }
                lineStart = i + 1;
            }
        }
        // handle a trailing line with no newline at the very end of the chunk, if any
        if (lineStart < end) {
            String line = new String(data, lineStart, end - lineStart, StandardCharsets.UTF_8);
            processLine(line, localStats);
        }

        return localStats;
    }

    private static void processLine(String line, Map<String, ServiceStats> statsByService) {
        String[] parts = line.split("\\|", 4);
        if (parts.length < 3) return;

        String level = parts[1];
        String service = parts[2];

        ServiceStats stats = statsByService.computeIfAbsent(service, k -> new ServiceStats());
        stats.total++;
        switch (level) {
            case "INFO" -> stats.infoCount++;
            case "WARN" -> stats.warnCount++;
            case "ERROR" -> stats.errorCount++;
        }
    }
}