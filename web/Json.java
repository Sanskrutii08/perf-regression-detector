import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON parser - just enough to read RunRegressionCheck.py's output
 * without pulling in a library.
 *
 * Objects become LinkedHashMap<String, Object>, arrays become
 * ArrayList<Object>, numbers become Double, plus String / Boolean / null.
 */
final class Json {

    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    static Object parse(String text) {
        Json p = new Json(text);
        p.ws();
        Object value = p.value();
        p.ws();
        if (p.i != p.s.length()) throw p.error("trailing characters");
        return value;
    }

    private Object value() {
        if (i >= s.length()) throw error("unexpected end of input");
        switch (s.charAt(i)) {
            case '{': return object();
            case '[': return array();
            case '"': return string();
            case 't': literal("true"); return Boolean.TRUE;
            case 'f': literal("false"); return Boolean.FALSE;
            case 'n': literal("null"); return null;
            default: return number();
        }
    }

    private Map<String, Object> object() {
        Map<String, Object> map = new LinkedHashMap<>();
        expect('{');
        ws();
        if (peek('}')) { i++; return map; }
        while (true) {
            ws();
            String key = string();
            ws();
            expect(':');
            ws();
            map.put(key, value());
            ws();
            if (peek(',')) { i++; continue; }
            expect('}');
            return map;
        }
    }

    private List<Object> array() {
        List<Object> list = new ArrayList<>();
        expect('[');
        ws();
        if (peek(']')) { i++; return list; }
        while (true) {
            ws();
            list.add(value());
            ws();
            if (peek(',')) { i++; continue; }
            expect(']');
            return list;
        }
    }

    private String string() {
        expect('"');
        StringBuilder b = new StringBuilder();
        while (true) {
            if (i >= s.length()) throw error("unterminated string");
            char c = s.charAt(i++);
            if (c == '"') return b.toString();
            if (c != '\\') { b.append(c); continue; }
            if (i >= s.length()) throw error("unterminated escape");
            char e = s.charAt(i++);
            switch (e) {
                case '"': case '\\': case '/': b.append(e); break;
                case 'b': b.append('\b'); break;
                case 'f': b.append('\f'); break;
                case 'n': b.append('\n'); break;
                case 'r': b.append('\r'); break;
                case 't': b.append('\t'); break;
                case 'u':
                    if (i + 4 > s.length()) throw error("bad unicode escape");
                    b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                    i += 4;
                    break;
                default: throw error("bad escape \\" + e);
            }
        }
    }

    private Double number() {
        int start = i;
        while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
        if (start == i) throw error("unexpected character '" + s.charAt(i) + "'");
        return Double.parseDouble(s.substring(start, i));
    }

    private void literal(String word) {
        if (!s.startsWith(word, i)) throw error("expected " + word);
        i += word.length();
    }

    private void ws() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++;
    }

    private boolean peek(char c) {
        return i < s.length() && s.charAt(i) == c;
    }

    private void expect(char c) {
        if (!peek(c)) throw error("expected '" + c + "'");
        i++;
    }

    private IllegalArgumentException error(String message) {
        return new IllegalArgumentException("Invalid JSON at position " + i + ": " + message);
    }
}
