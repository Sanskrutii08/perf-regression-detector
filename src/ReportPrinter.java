import java.lang.management.*;
import java.util.Map;
import java.util.concurrent.ThreadPoolExecutor;
import com.sun.management.OperatingSystemMXBean;

public class ReportPrinter {

    static void print(Map<String, ServiceStats> statsByService, long totalLines, long elapsedMs,
                      String label, ThreadPoolExecutor executor) {

        System.out.println("=== Log Aggregation Report: " + label + " ===");
        System.out.println("Elapsed time: " + elapsedMs + " ms");
        System.out.println("Throughput: " + (totalLines * 1000L / Math.max(elapsedMs, 1)) + " lines/sec");
        System.out.println();
        System.out.printf("%-25s %10s %10s %10s %10s %10s%n",
                "Service", "Total", "INFO", "WARN", "ERROR", "ErrRate%");

        long totalErrors = 0, totalAll = 0;

        for (var e : statsByService.entrySet()) {
            ServiceStats s = e.getValue();
            double errRate = s.total == 0 ? 0.0 : 100.0 * s.errorCount / s.total;
            System.out.printf("%-25s %10d %10d %10d %10d %9.2f%%%n",
                    e.getKey(), s.total, s.infoCount, s.warnCount, s.errorCount, errRate);
            totalErrors += s.errorCount;
            totalAll += s.total;
        }
        System.out.println();

        // ---- JVM-level metrics ----
        long gcPauseMs = 0, gcCount = 0;
        for (GarbageCollectorMXBean gc : ManagementFactory.getGarbageCollectorMXBeans()) {
            gcPauseMs += gc.getCollectionTime();
            gcCount += gc.getCollectionCount();
        }

        MemoryUsage heap = ManagementFactory.getMemoryMXBean().getHeapMemoryUsage();
        double heapUsagePct = 100.0 * heap.getUsed() / heap.getMax();

        ThreadMXBean threadBean = ManagementFactory.getThreadMXBean();
        long[] tids = threadBean.getAllThreadIds();
        long blocked = 0, waiting = 0;
        for (ThreadInfo info : threadBean.getThreadInfo(tids)) {
            if (info == null) continue;
            if (info.getThreadState() == Thread.State.BLOCKED) blocked++;
            else if (info.getThreadState() == Thread.State.WAITING
                    || info.getThreadState() == Thread.State.TIMED_WAITING) waiting++;
        }

        double cpuPct = 0.0;
        OperatingSystemMXBean osBean =
                (OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean();
        double load = osBean.getProcessCpuLoad(); // -1 if not available yet
        if (load >= 0) cpuPct = load * 100.0;

        double errorRatePct = totalAll == 0 ? 0.0 : 100.0 * totalErrors / totalAll;

        // ---- Machine-readable block for the Python side ----
        System.out.println("=== METRICS_JSON ===");
        System.out.printf("""
            {
              "total_lines": %d,
              "elapsed_ms": %d,
              "throughput_lps": %d,
              "gc_pause_ms": %d,
              "gc_count": %d,
              "heap_usage_pct": %.2f,
              "blocked_threads": %d,
              "waiting_threads": %d,
              "cpu_pct": %.2f,
              "error_rate_pct": %.2f
            }
            %n""",
                totalLines, elapsedMs, (totalLines * 1000L / Math.max(elapsedMs, 1)),
                gcPauseMs, gcCount, heapUsagePct, blocked, waiting, cpuPct, errorRatePct);
    }
}