import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;

/**
 * One-page web UI for the regression detector, using only the JDK's built-in
 * HTTP server and server-rendered HTML/CSS (no frameworks, no JavaScript).
 *
 * GET  /  shows the branch pickers.
 * POST /  runs src/RunRegressionCheck.py for the chosen branches, then renders
 *         the metrics, similar incidents, AI root cause / fix and code diff.
 *
 * The Python pipeline checks branches out in this repo, so only one
 * comparison runs at a time, and the server listens on localhost only.
 *
 * Build and run from the repo root:
 *   javac -d web/out web/*.java
 *   java -cp web/out PerfServer            (then open http://localhost:8080)
 *
 * Optional env vars: PORT (default 8080), PYTHON (default "python").
 */
public class PerfServer {

    private static final String MAIN_CLASS = "MultiThreadLogAggregator";
    private static final String LOG_FILE = "sample.log";
    private static final String THREADS = "8";
    private static final String DIFF_SCOPE = "*.java";
    private static final double VERDICT_THRESHOLD_PCT = 10.0; // same default as diff_engine.py

    private static final ReentrantLock RUN_LOCK = new ReentrantLock();

    private static Path repoDir;
    private static Path srcDir;
    private static String python;

    public static void main(String[] args) throws IOException {
        repoDir = Path.of(args.length > 0 ? args[0] : ".").toAbsolutePath().normalize();
        srcDir = repoDir.resolve("src");
        if (!Files.isRegularFile(srcDir.resolve("RunRegressionCheck.py"))) {
            System.err.println("src/RunRegressionCheck.py not found under " + repoDir
                    + " - run from the repo root or pass the repo path as the first argument.");
            System.exit(1);
        }
        python = System.getenv().getOrDefault("PYTHON", "python");
        int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8080"));

        HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), port), 0);
        server.createContext("/", PerfServer::handle);
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
        System.out.println("PerfGuard running at http://localhost:" + port + "/  (repo: " + repoDir + ")");
    }

    // -----------------------------------------------------------------------
    // HTTP
    // -----------------------------------------------------------------------

    private static void handle(HttpExchange ex) throws IOException {
        try {
            if (!ex.getRequestURI().getPath().equals("/")) {
                send(ex, 404, "text/plain", "Not found");
                return;
            }
            List<String> branches = branches();
            switch (ex.getRequestMethod()) {
                case "GET" -> send(ex, 200, "text/html",
                        page(branches, preferred(branches, "main", 0), preferred(branches, "dev", 1), null, null));
                case "POST" -> handleRun(ex, branches);
                default -> send(ex, 405, "text/plain", "Method not allowed");
            }
        } catch (Exception e) {
            send(ex, 500, "text/plain", "Server error: " + e);
        }
    }

    private static void handleRun(HttpExchange ex, List<String> branches) throws Exception {
        Map<String, String> form = parseForm(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
        String base = form.getOrDefault("base", "");
        String feature = form.getOrDefault("feature", "");

        String error = null;
        RunResult result = null;
        if (!branches.contains(base) || !branches.contains(feature)) {
            error = "Unknown branch.";
        } else if (base.equals(feature)) {
            error = "Pick two different branches.";
        } else if (!RUN_LOCK.tryLock()) {
            error = "Another comparison is already running. Try again when it finishes.";
        } else {
            try {
                result = runCheck(base, feature);
            } finally {
                RUN_LOCK.unlock();
            }
            error = result.error();
        }
        send(ex, 200, "text/html", page(branches, base, feature, result, error));
    }

    private static Map<String, String> parseForm(String body) {
        Map<String, String> form = new HashMap<>();
        for (String pair : body.split("&")) {
            int eq = pair.indexOf('=');
            if (eq <= 0) continue;
            form.put(URLDecoder.decode(pair.substring(0, eq), StandardCharsets.UTF_8),
                    URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8));
        }
        return form;
    }

    private static void send(HttpExchange ex, int status, String type, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", type + "; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(bytes);
        }
    }

    // -----------------------------------------------------------------------
    // Running the pipeline
    // -----------------------------------------------------------------------

    record RunResult(Map<String, Object> data, String baseSha, String featureSha, String stderr, String error) {}

    private static RunResult runCheck(String base, String feature) throws Exception {
        String baseSha = git("rev-parse", "--short", base).strip();
        String featureSha = git("rev-parse", "--short", feature).strip();

        Path out = Files.createTempFile("perfguard-", ".json");
        Path err = Files.createTempFile("perfguard-", ".log");
        try {
            ProcessBuilder pb = new ProcessBuilder(python, "RunRegressionCheck.py",
                    "--main", base, "--feature", feature,
                    "--main-class", MAIN_CLASS, "--log", LOG_FILE, "--threads", THREADS,
                    "--diff-dir", DIFF_SCOPE, "--analyze", "--out", out.toString())
                    .directory(srcDir.toFile())
                    .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                    .redirectError(err.toFile());
            pb.environment().put("PYTHONIOENCODING", "utf-8");
            Process process = pb.start();

            if (!process.waitFor(10, TimeUnit.MINUTES)) {
                process.destroyForcibly();
                return new RunResult(null, baseSha, featureSha, "", "The comparison timed out after 10 minutes.");
            }
            String stderr = new String(Files.readAllBytes(err), StandardCharsets.UTF_8);
            if (process.exitValue() != 0) {
                return new RunResult(null, baseSha, featureSha, stderr,
                        "The comparison failed (exit " + process.exitValue() + "):\n" + tail(stderr, 20));
            }
            Map<String, Object> data = map(Json.parse(Files.readString(out)));
            return new RunResult(data, baseSha, featureSha, stderr, null);
        } finally {
            Files.deleteIfExists(out);
            Files.deleteIfExists(err);
        }
    }

    private static List<String> branches() throws Exception {
        List<String> list = new ArrayList<>();
        for (String line : git("for-each-ref", "--format=%(refname:short)", "refs/heads").split("\\R")) {
            if (!line.isBlank()) list.add(line.strip());
        }
        return list;
    }

    private static String git(String... args) throws Exception {
        List<String> cmd = new ArrayList<>();
        cmd.add("git");
        cmd.addAll(List.of(args));
        Process p = new ProcessBuilder(cmd).directory(repoDir.toFile()).redirectErrorStream(true).start();
        String output = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        p.waitFor();
        return output;
    }

    private static String preferred(List<String> branches, String name, int fallbackIndex) {
        if (branches.contains(name)) return name;
        return branches.size() > fallbackIndex ? branches.get(fallbackIndex) : "";
    }

    private static String tail(String text, int lines) {
        String[] all = text.strip().split("\\R");
        return String.join("\n", List.of(all).subList(Math.max(0, all.length - lines), all.length));
    }

    // -----------------------------------------------------------------------
    // Page
    // -----------------------------------------------------------------------

    private enum Direction { HIGHER_IS_WORSE, LOWER_IS_WORSE, NEUTRAL, MUST_MATCH }

    private record Metric(String key, String label, Direction direction) {}

    private static final List<Metric> METRICS = List.of(
            new Metric("elapsed_ms", "Elapsed time (ms)", Direction.HIGHER_IS_WORSE),
            new Metric("throughput_lps", "Throughput (lines/s)", Direction.LOWER_IS_WORSE),
            new Metric("gc_pause_ms", "GC pause (ms)", Direction.HIGHER_IS_WORSE),
            new Metric("gc_count", "GC count", Direction.HIGHER_IS_WORSE),
            new Metric("heap_usage_pct", "Heap usage (%)", Direction.HIGHER_IS_WORSE),
            new Metric("blocked_threads", "Blocked threads", Direction.HIGHER_IS_WORSE),
            new Metric("waiting_threads", "Waiting threads", Direction.HIGHER_IS_WORSE),
            new Metric("cpu_pct", "CPU (%)", Direction.NEUTRAL),
            new Metric("total_lines", "Lines processed", Direction.MUST_MATCH),
            new Metric("error_rate_pct", "Error rate (%)", Direction.MUST_MATCH));

    private static String page(List<String> branches, String base, String feature, RunResult result, String error) {
        StringBuilder h = new StringBuilder();
        h.append("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
         .append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
         .append("<title>PerfGuard</title><style>").append(CSS).append("</style></head><body>")
         .append("<header><div class=\"wrap\"><strong>PerfGuard</strong>")
         .append("<span>Performance regression detector</span></div></header><main class=\"wrap\">");

        h.append("<form class=\"card picker\" method=\"post\" action=\"/\">")
         .append(select("base", "Baseline branch", branches, base))
         .append(select("feature", "Feature branch", branches, feature))
         .append("<button type=\"submit\">Run comparison</button>")
         .append("<p class=\"hint\">Builds and benchmarks both branches, finds similar incidents and asks the LLM. ")
         .append("Takes about a minute - the page loads when it is done.</p></form>");

        if (error != null) {
            h.append("<div class=\"alert\"><strong>").append(esc(firstLine(error))).append("</strong>");
            String rest = error.contains("\n") ? error.substring(error.indexOf('\n') + 1) : "";
            if (!rest.isBlank()) h.append("<pre>").append(esc(rest)).append("</pre>");
            h.append("</div>");
        }
        if (result != null && result.data() != null) {
            renderResults(h, base, feature, result);
        }
        return h.append("</main></body></html>").toString();
    }

    private static String select(String name, String label, List<String> branches, String selected) {
        StringBuilder s = new StringBuilder("<label>").append(label).append("<select name=\"").append(name).append("\">");
        for (String b : branches) {
            s.append("<option").append(b.equals(selected) ? " selected" : "").append(">").append(esc(b)).append("</option>");
        }
        return s.append("</select></label>").toString();
    }

    private static void renderResults(StringBuilder h, String base, String feature, RunResult r) {
        Map<String, Object> data = r.data();
        Map<String, Object> mBase = map(map(data.get("main")).get("metrics"));
        Map<String, Object> mFeat = map(map(data.get("feature")).get("metrics"));

        // ---- verdict ----
        Double elapsedPct = pct(num(mBase.get("elapsed_ms")), num(mFeat.get("elapsed_ms")));
        Double throughputPct = pct(num(mBase.get("throughput_lps")), num(mFeat.get("throughput_lps")));
        String verdict = "PASS";
        if ((throughputPct != null && throughputPct <= -VERDICT_THRESHOLD_PCT)
                || (elapsedPct != null && elapsedPct >= VERDICT_THRESHOLD_PCT)) verdict = "REGRESSION";
        else if ((throughputPct != null && throughputPct >= VERDICT_THRESHOLD_PCT)
                || (elapsedPct != null && elapsedPct <= -VERDICT_THRESHOLD_PCT)) verdict = "IMPROVEMENT";

        String headline = "No significant speed change";
        Double eBase = num(mBase.get("elapsed_ms")), eFeat = num(mFeat.get("elapsed_ms"));
        if (eBase != null && eFeat != null && eBase > 0 && eFeat > 0) {
            double ratio = eFeat / eBase;
            if (ratio >= 1.05) headline = fmt1(ratio) + "&times; slower";
            else if (ratio <= 0.95) headline = fmt1(1 / ratio) + "&times; faster";
        }
        if (throughputPct != null) headline += " &middot; throughput " + signedPct(throughputPct);

        h.append("<section class=\"card\"><div class=\"verdict-row\"><span class=\"badge ")
         .append(verdict.toLowerCase(Locale.ROOT)).append("\">").append(verdict).append("</span>")
         .append("<h1>").append(headline).append("</h1></div><p class=\"meta\">")
         .append("<code>").append(esc(base)).append(" @ ").append(esc(r.baseSha())).append("</code> &rarr; ")
         .append("<code>").append(esc(feature)).append(" @ ").append(esc(r.featureSha())).append("</code> &middot; ")
         .append(MAIN_CLASS).append(" &middot; ").append(LOG_FILE).append(" &middot; ").append(THREADS)
         .append(" threads &middot; regression if throughput drops or elapsed time rises by ")
         .append((int) VERDICT_THRESHOLD_PCT).append("% or more</p></section>");

        // ---- metrics ----
        h.append("<section class=\"card\"><h2>Metrics</h2><div class=\"scroll\"><table><thead><tr>")
         .append("<th>Metric</th><th class=\"num\">").append(esc(base)).append("</th><th class=\"num\">")
         .append(esc(feature)).append("</th><th class=\"num\">Change</th><th>Result</th></tr></thead><tbody>");
        for (Metric m : METRICS) {
            Double a = num(mBase.get(m.key())), b = num(mFeat.get(m.key()));
            if (a == null && b == null) continue;
            Double p = pct(a, b);
            String[] res = result(m.direction(), a, b, p);
            h.append("<tr><th scope=\"row\">").append(m.label()).append("</th>")
             .append("<td class=\"num\">").append(fmtNum(a)).append("</td>")
             .append("<td class=\"num\">").append(fmtNum(b)).append("</td>")
             .append("<td class=\"num ").append(res[1]).append("\">").append(p == null ? "new" : signedPct(p)).append("</td>")
             .append("<td><span class=\"pill ").append(res[1]).append("\">").append(res[0]).append("</span></td></tr>");
        }
        h.append("</tbody></table></div></section>");

        // ---- similar incidents ----
        List<Object> incidents = list(data.get("retrieved_incidents"));
        h.append("<section class=\"card\"><h2>Similar past incidents</h2>");
        if (incidents.isEmpty()) {
            h.append("<p class=\"muted\">No past incident was similar enough (cosine similarity &ge; 0.5).</p>");
        } else {
            h.append("<div class=\"incidents\">");
            for (Object o : incidents) {
                Map<String, Object> inc = map(o);
                double sim = num(inc.get("similarity")) == null ? 0 : num(inc.get("similarity"));
                h.append("<article class=\"incident\"><div class=\"sim\"><strong>")
                 .append(String.format(Locale.US, "%.3f", sim)).append("</strong><span class=\"bar\"><span style=\"width:")
                 .append(String.format(Locale.US, "%.1f", Math.max(0, Math.min(1, sim)) * 100)).append("%\"></span></span></div>")
                 .append("<div class=\"tags\">");
                for (Object tag : list(inc.get("tags"))) h.append("<span>").append(esc(str(tag))).append("</span>");
                h.append("</div><p><strong>Root cause.</strong> ").append(esc(str(inc.get("root_cause"))))
                 .append("</p><p><strong>Fix.</strong> ").append(esc(str(inc.get("fix")))).append("</p></article>");
            }
            h.append("</div>");
        }
        h.append("</section>");

        // ---- AI root cause + suggested fix ----
        Map<String, Object> llm = map(data.get("llm_analysis"));
        h.append("<section class=\"card\"><h2>AI root cause &amp; suggested fix</h2>");
        if (!llm.isEmpty()) {
            h.append("<p class=\"meta\">Model: <code>").append(esc(str(llm.get("provider_model")))).append("</code></p>")
             .append("<div class=\"ai\">").append(markdown(str(llm.get("pass2_final_analysis")))).append("</div>");
        } else {
            h.append("<p class=\"muted\">AI analysis was skipped");
            String reason = skipReason(r.stderr());
            if (reason != null) h.append(": ").append(esc(reason));
            h.append(".</p>");
        }
        h.append("</section>");

        // ---- code diff ----
        String diff = str(data.get("diff"));
        h.append("<section class=\"card\"><h2>Code diff <span class=\"muted\">(").append(esc(DIFF_SCOPE))
         .append(")</span></h2>");
        if (diff.isBlank()) {
            h.append("<p class=\"muted\">No code changes in these files.</p>");
        } else {
            h.append("<div class=\"scroll diff\"><pre>");
            for (String line : diff.split("\\R")) {
                String cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")
                        || line.startsWith("index ") ? "meta"
                        : line.startsWith("@@") ? "hunk"
                        : line.startsWith("+") ? "add"
                        : line.startsWith("-") ? "del" : "ctx";
                // each line is a block span, so no "\n" here - it would double the spacing inside <pre>
                h.append("<span class=\"").append(cls).append("\">").append(esc(line).isEmpty() ? " " : esc(line)).append("</span>");
            }
            h.append("</pre></div>");
        }
        h.append("</section>");
    }

    /** Returns {label, css class} for one metric row. */
    private static String[] result(Direction dir, Double a, Double b, Double pct) {
        if (a == null || b == null) return new String[] {"n/a", "same"};
        boolean same = pct != null ? Math.abs(pct) < 1.0 : a.equals(b);
        switch (dir) {
            case MUST_MATCH:
                return a.equals(b) ? new String[] {"Same", "same"} : new String[] {"Differs", "worse"};
            case NEUTRAL:
                if (same) return new String[] {"Same", "same"};
                return b > a ? new String[] {"Higher", "neutral"} : new String[] {"Lower", "neutral"};
            default:
                if (same) return new String[] {"Same", "same"};
                boolean worse = (dir == Direction.HIGHER_IS_WORSE) == (b > a);
                return worse ? new String[] {"Worse", "worse"} : new String[] {"Better", "better"};
        }
    }

    private static String skipReason(String stderr) {
        for (String line : stderr.split("\\R")) {
            int at = line.indexOf("skipping LLM analysis:");
            if (at >= 0) return line.substring(at + "skipping LLM analysis:".length()).strip();
        }
        return null;
    }

    /** Tiny markdown subset for LLM text: paragraphs, #-headings, bullet lists, **bold**, `code`. */
    private static String markdown(String text) {
        StringBuilder out = new StringBuilder();
        StringBuilder para = new StringBuilder();
        boolean inList = false;
        for (String raw : text.split("\\R")) {
            String t = raw.strip();
            boolean bullet = t.matches("^[-*]\\s+.*");
            if (t.isEmpty() || t.startsWith("#") || bullet) {
                if (para.length() > 0) { out.append("<p>").append(para).append("</p>"); para.setLength(0); }
            }
            if (!bullet && inList) { out.append("</ul>"); inList = false; }
            if (t.isEmpty()) continue;
            if (t.startsWith("#")) {
                out.append("<h3>").append(inline(t.replaceFirst("^#+\\s*", ""))).append("</h3>");
            } else if (bullet) {
                if (!inList) { out.append("<ul>"); inList = true; }
                out.append("<li>").append(inline(t.substring(1).strip())).append("</li>");
            } else {
                if (para.length() > 0) para.append("<br>");
                para.append(inline(t));
            }
        }
        if (para.length() > 0) out.append("<p>").append(para).append("</p>");
        if (inList) out.append("</ul>");
        return out.toString();
    }

    private static String inline(String s) {
        return esc(s).replaceAll("\\*\\*(.+?)\\*\\*", "<strong>$1</strong>").replaceAll("`([^`]+)`", "<code>$1</code>");
    }

    // -----------------------------------------------------------------------
    // Small helpers
    // -----------------------------------------------------------------------

    @SuppressWarnings("unchecked")
    private static Map<String, Object> map(Object o) {
        return o instanceof Map ? (Map<String, Object>) o : Map.of();
    }

    @SuppressWarnings("unchecked")
    private static List<Object> list(Object o) {
        return o instanceof List ? (List<Object>) o : List.of();
    }

    private static String str(Object o) {
        return o == null ? "" : o.toString();
    }

    private static Double num(Object o) {
        return o instanceof Double d ? d : null;
    }

    /** Percent change from a to b; null when a is 0 and b is not (undefined). */
    private static Double pct(Double a, Double b) {
        if (a == null || b == null) return null;
        if (a == 0) return b == 0 ? 0.0 : null;
        return (b - a) / a * 100.0;
    }

    private static String signedPct(double p) {
        String v = String.format(Locale.US, "%.1f%%", Math.abs(p));
        if (Math.abs(p) < 0.05) return v;
        return (p > 0 ? "+" : "&minus;") + v;
    }

    private static String fmtNum(Double v) {
        if (v == null) return "&ndash;";
        if (v == Math.rint(v) && Math.abs(v) < 1e15) return String.format(Locale.US, "%,d", v.longValue());
        return String.format(Locale.US, "%,.2f", v);
    }

    private static String fmt1(double v) {
        return String.format(Locale.US, "%.1f", v);
    }

    private static String firstLine(String s) {
        int nl = s.indexOf('\n');
        return nl < 0 ? s : s.substring(0, nl);
    }

    private static String esc(String s) {
        StringBuilder b = new StringBuilder(s.length());
        for (char c : s.toCharArray()) {
            switch (c) {
                case '<' -> b.append("&lt;");
                case '>' -> b.append("&gt;");
                case '&' -> b.append("&amp;");
                case '"' -> b.append("&quot;");
                case '\'' -> b.append("&#39;");
                default -> b.append(c);
            }
        }
        return b.toString();
    }

    private static final String CSS = """
        :root { --ink:#111827; --muted:#4b5563; --line:#e5e7eb; --bg:#f4f5f7; --card:#fff;
                --accent:#1f4fd1; --worse:#9a3412; --worse-bg:#fef0e6; --better:#1e40af; --better-bg:#eaf0fe; }
        * { box-sizing: border-box; }
        body { margin:0; background:var(--bg); color:var(--ink);
               font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
        code, pre, td.num, th.num { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; }
        .wrap { max-width:1100px; margin:0 auto; padding:0 16px; }
        header { background:var(--ink); color:#fff; }
        header .wrap { display:flex; gap:14px; align-items:baseline; padding:14px 16px; }
        header strong { font-size:18px; }
        header span { color:#d1d5db; font-size:14px; }
        main { display:flex; flex-direction:column; gap:16px; padding:20px 16px 48px; }
        .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; min-width:0; }
        h1 { margin:0; font-size:26px; letter-spacing:-0.01em; }
        h2 { margin:0 0 14px; font-size:18px; }
        h3 { margin:14px 0 6px; font-size:15px; }
        .muted { color:var(--muted); font-weight:400; }
        .meta { color:var(--muted); font-size:13px; margin:10px 0 0; }
        .meta code { background:#f3f4f6; border:1px solid var(--line); border-radius:6px; padding:1px 6px; color:var(--ink); }
        .picker { display:flex; flex-wrap:wrap; gap:16px; align-items:flex-end; }
        .picker label { display:flex; flex-direction:column; gap:6px; font-size:13px; font-weight:600; flex:1 1 220px; }
        select, button { font:inherit; min-height:44px; border-radius:8px; }
        select { padding:8px 10px; border:1px solid #d1d5db; background:#fff; }
        button { background:var(--accent); color:#fff; border:0; padding:10px 20px; font-weight:600; cursor:pointer; }
        button:hover { background:#163a9e; }
        .hint { flex-basis:100%; margin:0; color:var(--muted); font-size:13px; }
        .alert { background:var(--worse-bg); border:1px solid #f8c9a6; color:#7c2d12; border-radius:12px; padding:14px 18px; }
        .alert pre { white-space:pre-wrap; margin:8px 0 0; font-size:12px; }
        .verdict-row { display:flex; flex-wrap:wrap; gap:14px; align-items:center; }
        .badge { border-radius:999px; padding:4px 12px; font-size:13px; font-weight:700; letter-spacing:0.04em; border:1px solid; }
        .badge.regression { background:var(--worse-bg); color:var(--worse); border-color:#f8c9a6; }
        .badge.improvement { background:var(--better-bg); color:var(--better); border-color:#bfd0fb; }
        .badge.pass { background:#f3f4f6; color:#374151; border-color:#d1d5db; }
        .scroll { overflow-x:auto; }
        table { width:100%; border-collapse:collapse; min-width:560px; font-size:14px; }
        th, td { text-align:left; padding:10px 8px; border-bottom:1px solid #f3f4f6; }
        thead th { font-size:12px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); border-bottom:1px solid var(--line); }
        tbody th { font-weight:500; }
        .num { text-align:right; }
        td.worse { color:var(--worse); font-weight:600; }
        td.better { color:var(--better); font-weight:600; }
        .pill { display:inline-block; border-radius:999px; padding:2px 10px; font-size:12px; font-weight:600; background:#f3f4f6; color:#374151; }
        .pill.worse { background:var(--worse-bg); color:var(--worse); }
        .pill.better { background:var(--better-bg); color:var(--better); }
        .incidents { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:14px; }
        .incident { border:1px solid var(--line); border-radius:10px; padding:16px; }
        .incident p { margin:8px 0 0; font-size:14px; }
        .sim { display:flex; align-items:center; gap:10px; }
        .sim strong { font-size:22px; }
        .bar { flex:1; height:6px; background:var(--line); border-radius:3px; overflow:hidden; }
        .bar span { display:block; height:100%; background:var(--accent); }
        .tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
        .tags span { font-family:ui-monospace, Consolas, monospace; font-size:12px; background:#eef2ff; color:#1e3a8a; border-radius:4px; padding:2px 6px; }
        .ai { font-size:15px; }
        .ai p { margin:0 0 10px; }
        .ai code { background:#f3f4f6; border-radius:4px; padding:1px 4px; font-size:13px; }
        .diff { border:1px solid var(--line); border-radius:8px; background:#fbfbfc; }
        .diff pre { margin:0; padding:8px 0; font-size:13px; line-height:1.6; }
        .diff span { display:block; padding:0 14px; white-space:pre; }
        .diff .add { background:#e8f3ec; color:#14532d; }
        .diff .del { background:#fdecec; color:#7f1d1d; }
        .diff .hunk { background:#f3f4f6; color:var(--muted); }
        .diff .meta { color:var(--muted); font-weight:600; }
        """;
}
