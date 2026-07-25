import java.util.Map;

public class ReportPrinter {
    static void print(Map<String, ServiceStats> statsByService, long totalLines, long elapsedMs, String label) {
        System.out.println("=== Log Aggregation Report: " + label + " ===");
        System.out.println("Total lines processed: " + totalLines);
        System.out.println("Elapsed time: " + elapsedMs + " ms");
        System.out.println("Throughput: " + (totalLines * 1000L / Math.max(elapsedMs, 1)) + " lines/sec");
        System.out.println();
        System.out.printf("%-25s %10s %10s %10s %10s %10s%n",
                "Service", "Total", "INFO", "WARN", "ERROR", "ErrRate%");

        statsByService.entrySet().stream()
                .sorted(Map.Entry.comparingByKey())
                .forEach(e -> {
                    ServiceStats s = e.getValue();
                    double errRate = s.total == 0 ? 0.0 : 100.0 * s.errorCount / s.total;
                    System.out.printf("%-25s %10d %10d %10d %10d %9.2f%%%n",
                            e.getKey(), s.total, s.infoCount, s.warnCount, s.errorCount, errRate);
                });
        System.out.println();
    }
}