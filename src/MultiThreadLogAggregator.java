import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * V2 variant - in-memory processing, "simplified" to run without a thread pool.
 *
 * Reads the entire file into a byte[] ONCE, still splits it into the same
 * line-aligned chunks, but then processes those chunks one after another on
 * the main thread instead of handing them to worker threads.
 *
 * REGRESSION EXAMPLE (reduced parallelism): the work is identical to main,
 * but only one core is ever used, so elapsed time grows roughly with the
 * number of chunks and throughput drops, while CPU usage falls.
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

        // Process the chunks sequentially on this thread - no worker pool.
        List<Map<String, ServiceStats>> partials = new ArrayList<>();
        for (int i = 0; i < numThreads; i++) {
            partials.add(processChunk(data, boundaries[i], boundaries[i + 1]));
        }

        long t3 = System.nanoTime(); // all chunks processed

        Map<String, ServiceStats> merged = new HashMap<>();
        for (Map<String, ServiceStats> local : partials) {
            for (Map.Entry<String, ServiceStats> e : local.entrySet()) {
                merged.computeIfAbsent(e.getKey(), k -> new ServiceStats()).merge(e.getValue());
            }
        }
        long t4 = System.nanoTime(); // merged

        long totalLines = 0;
        for (ServiceStats s : merged.values()) totalLines += s.total;

        long overallElapsedMs = (System.nanoTime() - overallStart) / 1_000_000;

        System.out.printf("[timing] file read into memory: %d ms%n", (t1 - t0) / 1_000_000);
        System.out.printf("[timing] boundary computation: %d ms%n", (t2 - t1) / 1_000_000);
        System.out.printf("[timing] sequential chunk processing: %d ms%n", (t3 - t2) / 1_000_000);
        System.out.printf("[timing] merge: %d ms%n", (t4 - t3) / 1_000_000);
        System.out.println();

        ReportPrinter.print(merged, totalLines, overallElapsedMs, "V2-in-memory (single-threaded, " + numThreads + " chunks)", null);
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