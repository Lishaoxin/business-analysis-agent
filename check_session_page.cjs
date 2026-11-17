// 使用Node内置模块检查实际页面脚本，不访问模型、网络或数据库。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { TextDecoder } = require("node:util");

const filename = process.argv[2] || path.join(__dirname, "index.html");
const html = fs.readFileSync(filename, "utf8");
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];
assert.equal(scripts.length, 1);
let uuidCounter = 0;
const tick = () => new Promise(resolve => setImmediate(resolve));

class Element {
    constructor(tagName) {
        this.tagName = tagName;
        this.children = [];
        this.listeners = {};
        this.style = {};
        this.disabled = false;
        this.value = "";
        this._text = "";
    }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    set innerHTML(value) { throw new Error("问答和工具数据应使用textContent"); }
    replaceChildren(...children) { this._text = ""; this.children = children; }
    appendChild(child) { this.children.push(child); return child; }
    addEventListener(event, callback) { this.listeners[event] = callback; }
    focus() {}
}

function makeBody() {
    const chunks = [], waiting = [];
    let ended = false, failure = null;
    const stats = { cancelled: 0, released: 0 };
    function next() {
        if (failure) return Promise.reject(failure);
        if (chunks.length) return Promise.resolve({ value: chunks.shift(), done: false });
        if (ended) return Promise.resolve({ done: true });
        return new Promise((resolve, reject) => waiting.push({ resolve, reject }));
    }
    function flush() {
        while (waiting.length && (chunks.length || ended || failure)) {
            const waiter = waiting.shift();
            next().then(waiter.resolve, waiter.reject);
        }
    }
    const reader = {
        read: next,
        async cancel() { stats.cancelled += 1; ended = true; flush(); if (failure) throw failure; },
        releaseLock() { stats.released += 1; }
    };
    return {
        stats,
        body: { getReader() { return reader; } },
        push(value) { chunks.push(typeof value === "string" ? Buffer.from(value) : value); flush(); },
        end() { ended = true; flush(); },
        fail(error) { failure = error; chunks.length = 0; flush(); }
    };
}

function createPage(storage = new Map()) {
    const elements = new Map();
    for (const match of html.matchAll(/<([a-z][a-z0-9-]*)\b[^>]*\bid="([^"]+)"/gi)) {
        assert.ok(!elements.has(match[2]), "元素ID不可重复");
        elements.set(match[2], new Element(match[1]));
    }
    const calls = [], pending = [];
    const context = vm.createContext({
        TextDecoder,
        document: {
            getElementById(id) { return elements.get(id) || null; },
            createElement(tagName) { return new Element(tagName); }
        },
        sessionStorage: {
            getItem(key) { return storage.has(key) ? storage.get(key) : null; },
            setItem(key, value) { storage.set(key, String(value)); }
        },
        crypto: { randomUUID() { return "00000000-0000-4000-8000-" + String(++uuidCounter).padStart(12, "0"); } },
        fetch(url, options) {
            calls.push({ url, options, body: JSON.parse(options.body) });
            return new Promise((resolve, reject) => pending.push({ resolve, reject }));
        }
    });
    vm.runInContext(scripts[0][1], context, { filename });
    return {
        elements, calls, storage,
        id() { assert.equal(storage.size, 1); return [...storage.values()][0]; },
        fireSubmit() { return elements.get("analysis-form").listeners.submit({ preventDefault() {} }); },
        submit(question) { elements.get("question").value = question; return this.fireSubmit(); },
        startNewSession() { elements.get("new-session-button").listeners.click(); },
        openStream(contentType = "text/event-stream; charset=utf-8") {
            assert.ok(pending.length);
            const pipe = makeBody();
            pending.shift().resolve({
                ok: true, headers: { get() { return contentType; } }, body: pipe.body,
                async json() { throw new Error("SSE成功响应不应调用response.json()"); }
            });
            return pipe;
        },
        replyError(detail) {
            assert.ok(pending.length);
            pending.shift().resolve({ ok: false, async json() { return { detail }; } });
        },
        reject(error) { assert.ok(pending.length); pending.shift().reject(error); },
        read(response, callback) { return context.readSse(response, callback); }
    };
}

function frame(event, newline = "\n", pretty = false) {
    const lines = JSON.stringify(event.data, null, pretty ? 2 : 0).split("\n");
    return ["event: " + event.event, ...lines.map(line => "data: " + line), "", ""].join(newline);
}

function result(threadId, overrides = {}) {
    return { thread_id: threadId, status: "completed", answer: "预设回答", evidence_query_ids: ["query_example"], steps: [], charts: [], ...overrides };
}

function tool(callId = "call_example", overrides = {}) {
    return { round: 2, tool_call_id: callId, tool_name: "run_sql", raw_arguments: "{}", ...overrides };
}

function assertEnabled(page, enabled) {
    for (const id of ["submit-button", "new-session-button", "question"]) {
        assert.equal(page.elements.get(id).disabled, !enabled, id);
    }
}

async function checkParser() {
    const events = [
        { event: "tool_start", data: tool("call_中文", { tool_name: "示例工具" }) },
        { event: "tool_result", data: tool("call_中文", { result: { ok: true, data: { rows: [["键盘", 1400]] } } }) },
        { event: "final", data: result("parser-A", { answer: "中文回答\n第二行：九月与十月。" }) }
    ];
    const page = createPage();
    for (const newline of ["\n", "\r\n", "\r"]) {
        const pipe = makeBody(), received = [];
        const running = page.read({ body: pipe.body }, event => received.push(JSON.parse(JSON.stringify(event))));
        const bytes = Buffer.from(": ping" + newline + newline + "id: 1" + newline + "retry: 5000" + newline +
            events.map(event => frame(event, newline, true)).join(""));
        for (let index = 0; index < bytes.length; index++) pipe.push(bytes.subarray(index, index + 1));
        pipe.end();
        await running;
        assert.deepEqual(received, events);
        assert.equal(pipe.stats.released, 1);
    }
    print("逐字节分块、中文UTF-8、LF/CRLF/CR换行、心跳注释和多行data均能解析");
}

async function main() {
    await checkParser();
    const page = createPage(), firstId = page.id();
    let running = page.submit("  2026年9月的已支付销售额是多少元？  ");
    assert.equal(page.calls[0].url, "/analyze/stream");
    assert.equal(page.calls[0].options.method, "POST");
    assert.equal(page.calls[0].options.headers.Accept, "text/event-stream");
    assert.deepEqual(page.calls[0].body, { question: "2026年9月的已支付销售额是多少元？", thread_id: firstId });
    assertEnabled(page, false);
    page.startNewSession();
    await page.fireSubmit();
    assert.equal(page.id(), firstId);
    assert.equal(page.calls.length, 1);
    let pipe = page.openStream();
    pipe.push(": ping\n\n");
    await tick();
    assert.equal(page.elements.get("steps").children.length, 0);

    const bad = tool("call_bad");
    pipe.push(frame({ event: "tool_start", data: bad }));
    await tick();
    assert.ok(page.elements.get("steps").textContent.includes("执行中"));
    assert.equal(page.elements.get("conversation").children.length, 0);
    assertEnabled(page, false);
    pipe.push(frame({ event: "tool_start", data: bad }));
    await tick();
    assert.equal(page.elements.get("steps").children.length, 1);
    const badResult = { ...bad, result: { ok: false, error: "错误字段，可继续修复" } };
    pipe.push(frame({ event: "tool_result", data: badResult }));
    await tick();
    assert.ok(page.elements.get("steps").textContent.includes("失败"));
    assertEnabled(page, false);
    const fixed = tool("call_fixed", { round: 3 });
    const goodResult = { ...fixed, result: { ok: true, data: { columns: ["amount"], rows: [[1400]] } } };
    pipe.push(frame({ event: "tool_start", data: fixed }) + frame({ event: "tool_result", data: goodResult }));
    await tick();
    assert.equal(page.elements.get("steps").children.length, 2);
    assert.equal(page.elements.get("conversation").children.length, 0);
    print("final到达前工具进度已显示；开始与结果按调用编号更新，工具失败不会提前结束");

    const final = { event: "final", data: result(firstId, {
        answer: "9月：1400元", steps: [badResult, goodResult], charts: ["http://127.0.0.1:8001/outputs/example.png"]
    }) };
    pipe.push(frame(final) + frame(final));
    await running; // 不发送EOF：收到final就应结束读取。
    assertEnabled(page, true);
    assert.equal(page.elements.get("conversation").children.length, 1);
    assert.equal(page.elements.get("steps").children.length, 2);
    assert.equal(page.elements.get("status").textContent, "分析完成");
    assert.equal(page.elements.get("charts").children[0].src, final.data.charts[0]);
    assert.equal(page.elements.get("question").value, "");
    assert.equal(pipe.stats.cancelled, 1);
    assert.equal(pipe.stats.released, 1);
    print("final只展示一次，正式回答、完整工具记录、图表和读取资源清理正常");

    running = page.submit("那10月份呢？");
    assert.equal(page.calls.at(-1).body.thread_id, firstId);
    assert.equal(page.elements.get("steps").children.length, 0);
    assert.equal(page.elements.get("charts").children.length, 0);
    pipe = page.openStream();
    pipe.push(frame({ event: "final", data: result(firstId, { answer: "10月：1700元" }) }));
    await running;
    assert.equal(page.elements.get("conversation").children.length, 2);
    assert.ok(page.elements.get("conversation").textContent.includes("9月：1400元"));
    assert.ok(page.elements.get("conversation").textContent.includes("10月：1700元"));
    const reloaded = createPage(page.storage);
    assert.equal(reloaded.id(), firstId);
    assert.equal(reloaded.elements.get("conversation").children.length, 0);
    print("连续追问沿用编号并保留问答，模拟刷新后编号仍然保留");

    for (const [taskStatus, label] of [["needs_clarification", "需要补充信息"], ["failed", "本次分析未完成"]]) {
        running = page.submit("业务状态检查");
        pipe = page.openStream();
        pipe.push(frame({ event: "final", data: result(firstId, { status: taskStatus, evidence_query_ids: [] }) }));
        await running;
        assert.equal(page.elements.get("status").textContent, label);
    }
    print("final中的completed、needs_clarification与failed分别显示正确状态");

    const historyCount = page.elements.get("conversation").children.length;
    running = page.submit("保留这条问题");
    pipe = page.openStream();
    pipe.push(frame({ event: "tool_start", data: tool() }));
    await tick();
    pipe.push(frame({ event: "error", data: { thread_id: firstId, message: "模型请求超时", status_code: 504 } }));
    await running;
    assert.ok(page.elements.get("status").textContent.includes("模型请求超时"));
    assert.ok(page.elements.get("steps").textContent.includes("未收到结果"));
    assert.equal(page.elements.get("question").value, "保留这条问题");
    assert.equal(page.elements.get("conversation").children.length, historyCount);
    assert.equal(page.id(), firstId);
    assertEnabled(page, true);
    print("error事件显示故障，保留已有进度、旧问答、输入与会话编号");

    for (const mode of ["eof", "network"]) {
        running = page.submit("断线后保留输入");
        pipe = page.openStream();
        pipe.push(frame({ event: "tool_start", data: tool() }));
        await tick();
        const before = page.calls.length;
        if (mode === "eof") pipe.end(); else pipe.fail(new Error("网络连接中断"));
        await running;
        assert.ok(page.elements.get("status").textContent.startsWith("请求失败："));
        assert.ok(page.elements.get("steps").textContent.includes("未收到结果"));
        assert.equal(page.elements.get("conversation").children.length, historyCount);
        assert.equal(page.elements.get("question").value, "断线后保留输入");
        assert.equal(page.calls.length, before);
        assert.equal(pipe.stats.released, 1);
        assertEnabled(page, true);
    }
    print("缺少final时的EOF或网络异常不会误报完成，也不会自动重发任务");

    running = page.submit("HTTP错误");
    page.replyError("问题不能为空");
    await running;
    assert.ok(page.elements.get("status").textContent.includes("问题不能为空"));
    running = page.submit("连接建立失败");
    page.reject(new Error("连接失败"));
    await running;
    assert.ok(page.elements.get("status").textContent.includes("连接失败"));
    running = page.submit("错误响应类型");
    pipe = page.openStream("application/json");
    await running;
    assert.ok(page.elements.get("status").textContent.includes("事件流"));
    assertEnabled(page, true);
    print("HTTP校验错误、连接失败和错误响应类型均能恢复操作");

    for (const event of [
        { event: "unknown", data: {} },
        { event: "final", data: result("wrong-thread") },
        { event: "final", data: result(firstId, { status: "done" }) },
        { event: "tool_result", data: [] },
    ]) {
        running = page.submit("格式错误检查");
        pipe = page.openStream();
        pipe.push(frame(event));
        await running;
        assert.ok(page.elements.get("status").textContent.startsWith("请求失败："));
        assert.equal(page.id(), firstId);
        assert.equal(page.elements.get("conversation").children.length, historyCount);
        assertEnabled(page, true);
    }
    running = page.submit("不完整final");
    pipe = page.openStream();
    pipe.push(frame({ event: "final", data: result(firstId) }).slice(0, -1));
    pipe.end();
    await running;
    assert.equal(page.elements.get("conversation").children.length, historyCount);
    assert.ok(page.elements.get("status").textContent.includes("未收到最终结果"));
    print("未知事件、错误会话编号、错误结束格式及不完整事件不会被当成成功");

    const text = '<img src=x onerror="unexpected()">';
    running = page.submit(text);
    pipe = page.openStream();
    pipe.push(frame({ event: "final", data: result(firstId, { answer: text }) }));
    await running;
    assert.equal(page.elements.get("conversation").children.at(-1).children[1].textContent, text);
    print("问题、回答与工具内容继续按文本展示");

    page.startNewSession();
    const newId = page.id();
    assert.notEqual(newId, firstId);
    for (const id of ["conversation", "steps", "charts"]) {
        assert.equal(page.elements.get(id).textContent, "");
        assert.equal(page.elements.get(id).children.length, 0);
    }
    running = page.submit("新的第一问");
    assert.equal(page.calls.at(-1).body.thread_id, newId);
    pipe = page.openStream();
    pipe.push(frame({ event: "final", data: result(newId) }));
    await running;
    const before = page.calls.length;
    await page.submit("   ");
    assert.equal(page.calls.length, before);
    print("新会话换编号并清空页面，空白问题不发请求，请求中禁止切换或重复提交");
    print("网页SSE与会话逻辑检查通过（预设字节流，未请求真实API）");
}

function print(message) { process.stdout.write(message + "\n"); }
main().catch(error => { process.stderr.write(String(error.stack || error) + "\n"); process.exitCode = 1; });
