import java.io.BufferedReader;
import java.io.File;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.RandomAccessFile;
import java.nio.channels.Channels;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.*;

/**
 * V2 - multithreaded, chunked processing.
 *
 * The file is split into N byte-range chunks, aligned to line boundaries
 * so no line is ever split across two threads. Each worker thread reads
 * only its own chunk and builds its OWN private Map<String, ServiceStats> -
 * there is no shared mutable state and therefore no locking during the hot
 * path. Partial results are merged single-threaded at the very end, once
 * all workers are done.
 *
 * This "local-then-merge" pattern is deliberate: it is the correct way to
 * parallelize this kind of aggregation, and it is also the pattern a later
 * "regression" version could break - e.g. by replacing per-thread local
 * maps with one shared map guarded by a single global lock, which would
 * serialize the workers and tank throughput despite the pool still being
 * multithreaded on paper.
 *
 * Usage: java LogAggregatorV2 <path-to-log-file> [num-threads]
 */
public class MultiThreadLogAggregator {

    public static void main(String[] args) throws Exception {
        String path = args.length > 0 ? args[0] : "sample.log";
        int numThreads = args.length > 1 ? Integer.parseInt(args[1]) : Runtime.getRuntime().availableProcessors();

        long overallStart = System.nanoTime();

        File file = new File(path);
        long fileSize = file.length();

        long t0 = System.nanoTime();
        long[] boundaries = computeChunkBoundaries(file, fileSize, numThreads);
        long t1 = System.nanoTime();

        ExecutorService pool = Executors.newFixedThreadPool(numThreads);
        List<Future<Map<String, ServiceStats>>> futures = new ArrayList<>();

        for (int i = 0; i < numThreads; i++) {
            final long chunkStart = boundaries[i];
            final long chunkEnd = boundaries[i + 1];
            futures.add(pool.submit(() -> processChunk(path, chunkStart, chunkEnd)));
        }

        long t2 = System.nanoTime(); // all tasks submitted, workers running now

        Map<String, ServiceStats> merged = new HashMap<>();
        for (Future<Map<String, ServiceStats>> future : futures) {
            Map<String, ServiceStats> local = future.get(); // blocks until that worker is done
            for (Map.Entry<String, ServiceStats> e : local.entrySet()) {
                merged.computeIfAbsent(e.getKey(), k -> new ServiceStats()).merge(e.getValue());
            }
        }
        long t3 = System.nanoTime(); // all workers finished + merged

        pool.shutdown();

        long totalLines = 0;
        for (ServiceStats s : merged.values()) totalLines += s.total;

        long overallElapsedMs = (System.nanoTime() - overallStart) / 1_000_000;

        System.out.printf("[timing] boundary computation: %d ms%n", (t1 - t0) / 1_000_000);
        System.out.printf("[timing] pool creation + submit: %d ms%n", (t2 - t1) / 1_000_000);
        System.out.printf("[timing] worker processing + merge: %d ms%n", (t3 - t2) / 1_000_000);
        System.out.println();

        ReportPrinter.print(merged, totalLines, overallElapsedMs, "V2 (multithreaded, " + numThreads + " threads)",(ThreadPoolExecutor) pool);
    }

    /**
     * Finds byte offsets that divide the file into roughly equal chunks,
     * nudging each boundary forward to the start of the next full line so
     * a chunk never begins mid-line.
     */
    private static long[] computeChunkBoundaries(File file, long fileSize, int numChunks) throws IOException {
        long[] boundaries = new long[numChunks + 1];
        boundaries[0] = 0;
        boundaries[numChunks] = fileSize;

        try (RandomAccessFile raf = new RandomAccessFile(file, "r")) {
            for (int i = 1; i < numChunks; i++) {
                long approx = (fileSize * i) / numChunks;
                raf.seek(approx);
                long pos = approx;
                int b;
                while ((b = raf.read()) != -1 && b != '\n') {
                    pos++;
                }
                boundaries[i] = pos + 1; // start right after the newline
            }
        }
        return boundaries;
    }

    /**
     * Reads and aggregates only this thread's byte range into a local, unshared map.
     *
     * IMPORTANT: this uses a buffered stream on top of the file channel, NOT
     * RandomAccessFile.readLine(). RandomAccessFile's own readLine() reads one
     * byte at a time under the hood - for a multi-MB file that turns into
     * millions of tiny reads and completely dominates the runtime, making the
     * "multithreaded" version slower than a plain BufferedReader despite the
     * parallelism. Wrapping the channel in a BufferedReader restores normal
     * chunked I/O (default 8KB buffer) so the threads are actually
     * CPU/parse-bound instead of I/O-call-bound.
     */
    private static Map<String, ServiceStats> processChunk(String path, long start, long end) throws IOException {
        Map<String, ServiceStats> localStats = new HashMap<>();

        try (RandomAccessFile raf = new RandomAccessFile(path, "r");
             FileChannel channel = raf.getChannel()) {

            channel.position(start);
            // Cap the stream at this chunk's end so the reader stops exactly at the
            // boundary. Counting bytes from readLine() is unreliable: it strips the
            // line terminator, so "\r\n" files would be under-counted and the thread
            // would overrun into (and double-count) the next chunk.
            InputStream in = limit(Channels.newInputStream(channel), end - start);
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8), 1 << 16);

            String line;
            while ((line = reader.readLine()) != null) {
                processLine(line, localStats);
            }
        }
        return localStats;
    }

    /** Wraps a stream so that at most {@code limit} bytes can be read from it. */
    private static InputStream limit(InputStream in, long limit) {
        return new FilterInputStream(in) {
            private long remaining = limit;

            @Override
            public int read() throws IOException {
                if (remaining <= 0) return -1;
                int b = super.read();
                if (b != -1) remaining--;
                return b;
            }

            @Override
            public int read(byte[] buf, int off, int len) throws IOException {
                if (remaining <= 0) return -1;
                int n = super.read(buf, off, (int) Math.min(len, remaining));
                if (n > 0) remaining -= n;
                return n;
            }
        };
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