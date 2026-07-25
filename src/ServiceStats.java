/**
 * Aggregated counts for a single service, built up while scanning the log.
 * Deliberately a plain mutable object with no synchronization - in V2, each
 * worker thread gets its own private instance per service and merges are
 * done single-threaded at the end, so no locking is needed here at all.
 */
public class ServiceStats {
    long total = 0;
    long infoCount = 0;
    long warnCount = 0;
    long errorCount = 0;

    void merge(ServiceStats other) {
        total += other.total;
        infoCount += other.infoCount;
        warnCount += other.warnCount;
        errorCount += other.errorCount;
    }
}