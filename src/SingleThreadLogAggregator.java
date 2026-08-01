import java.io.BufferedReader;
import java.io.FileReader;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
//import

/**
 * V1 - single-threaded baseline.
 *
 * Reads the log file line by line on the main thread, parsing and
 * aggregating each line sequentially. This is the "before" version:
 * simple, correct, and throughput is bounded by a single core no matter
 * how large the file gets.
 *
 * Usage: java SingleThreadLogAggregator <path-to-log-file>
 */
public class SingleThreadLogAggregator {

    public static void main(String[] args) throws IOException {
        String path = args.length > 0 ? args[0] : "sample.log";

        long start = System.nanoTime();

        Map<String, ServiceStats> statsByService = new HashMap<>();
        long totalLines = 0;

        try (BufferedReader reader = new BufferedReader(new FileReader(path))) {
            String line;
            while ((line = reader.readLine()) != null) {
                processLine(line, statsByService);
                totalLines++;
            }
        }

        long elapsedMs = (System.nanoTime() - start) / 1_000_000;

        ReportPrinter.print(statsByService, totalLines, elapsedMs, "V1 (single-threaded)",null);
    }

    private static void processLine(String line, Map<String,ServiceStats> statsByService) {
        // format: timestamp|level|service|message
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