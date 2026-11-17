// 仅使用Node内置模块执行页面脚本，不访问网络、模型或数据库。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const filename = process.argv[2] || path.join(__dirname, "index.html");
const html = fs.readFileSync(filename, "utf8");
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];
assert.equal(scripts.length, 1, "页面应有一份内联业务脚本");
let uuidCounter = 0;

class Element {
    constructor(tagName) {
        this.tagName = tagName;
        this.children = [];
        this.listeners = {};
        this.style = {};
        this.disabled = false;
        this.value = "";
        this._text = "";
        this.focusCount = 0;
    }

    set textContent(value) {
        this._text = String(value);
        this.children = [];
    }

    get textContent() {
        return this._text + this.children.map(child => child.textContent).join("");
    }

    set innerHTML(value) {
        throw new Error("问答与工具结果应作为文本展示，不使用innerHTML");
    }

    replaceChildren(...children) {
        this._text = "";
        this.children = children;
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    addEventListener(event, callback) {
        this.listeners[event] = callback;
    }

    focus() {
        this.focusCount += 1;
    }
}

function createPage(storage = new Map()) {
    const elements = new Map();
    for (const match of html.matchAll(/<([a-z][a-z0-9-]*)\b[^>]*\bid="([^"]+)"/gi)) {
        elements.set(match[2], new Element(match[1]));
    }
    const calls = [];
    const pending = [];
    vm.runInNewContext(scripts[0][1], {
        document: {
            getElementById(id) { return elements.get(id) || null; },
            createElement(tagName) { return new Element(tagName); }
        },
        sessionStorage: {
            getItem(key) { return storage.has(key) ? storage.get(key) : null; },
            setItem(key, value) { storage.set(key, String(value)); }
        },
        crypto: {
            randomUUID() {
                uuidCounter += 1;
                return "00000000-0000-4000-8000-" + String(uuidCounter).padStart(12, "0");
            }
        },
        fetch(url, options) {
            calls.push({ url, options, body: JSON.parse(options.body) });
            return new Promise((resolve, reject) => pending.push({ resolve, reject }));
        }
    }, { filename });

    return {
        elements,
        calls,
        storage,
        id() {
            assert.equal(storage.size, 1, "这里只需在页面存储会话编号");
            return [...storage.values()][0];
        },
        submit(question) {
            elements.get("question").value = question;
            return elements.get("analysis-form").listeners.submit({ preventDefault() {} });
        },
        startNewSession() {
            return elements.get("new-session-button").listeners.click();
        },
        reply(data, ok = true) {
            assert.ok(pending.length > 0, "应先发送请求，再提供预设响应");
            pending.shift().resolve({ ok, json: async () => data });
        },
        reject(error) {
            assert.ok(pending.length > 0);
            pending.shift().reject(error);
        }
    };
}

function result(threadId, overrides = {}) {
    return {
        thread_id: threadId,
        status: "completed",
        answer: "预设回答",
        steps: [],
        charts: [],
        ...overrides
    };
}

async function main() {
    const page = createPage();
    const firstId = page.id();
    assert.ok(firstId);
    let running = page.submit("  2026年9月的已支付销售额是多少元？  ");
    assert.equal(page.calls.length, 1);
    assert.equal(page.calls[0].url, "/analyze");
    assert.equal(page.calls[0].options.method, "POST");
    assert.equal(page.calls[0].options.headers["Content-Type"], "application/json");
    assert.deepEqual(page.calls[0].body, {
        question: "2026年9月的已支付销售额是多少元？", thread_id: firstId
    });
    print("首问发送前已保存编号，请求同时携带question和thread_id");

    for (const id of ["submit-button", "new-session-button", "question"]) {
        assert.equal(page.elements.get(id).disabled, true);
    }
    page.startNewSession();
    await page.elements.get("analysis-form").listeners.submit({ preventDefault() {} });
    assert.equal(page.id(), firstId);
    assert.equal(page.calls.length, 1);
    page.reply(result(firstId, {
        answer: "9月：1400元",
        steps: [{ round: 1, tool_name: "run_sql", result: { ok: true, data: {} } }],
        charts: ["http://127.0.0.1:8001/outputs/example.png"]
    }));
    await running;
    for (const id of ["submit-button", "new-session-button", "question"]) {
        assert.equal(page.elements.get(id).disabled, false);
    }
    assert.equal(page.elements.get("question").value, "");
    assert.equal(page.elements.get("status").textContent, "分析完成");
    assert.equal(page.elements.get("steps").children[0].tagName, "details");
    assert.equal(page.elements.get("charts").children[0].src,
        "http://127.0.0.1:8001/outputs/example.png");
    print("请求处理中不能重复提交或切换会话，结束后恢复操作");

    running = page.submit("那10月份呢？");
    assert.equal(page.calls[1].body.thread_id, firstId);
    assert.equal(page.elements.get("conversation").children.length, 1);
    assert.equal(page.elements.get("steps").children.length, 0);
    assert.equal(page.elements.get("charts").children.length, 0);
    page.reply(result(firstId, { answer: "10月：1700元" }));
    await running;
    assert.equal(page.elements.get("conversation").children.length, 2);
    assert.ok(page.elements.get("conversation").textContent.includes("9月：1400元"));
    assert.ok(page.elements.get("conversation").textContent.includes("10月：1700元"));
    print("连续两问沿用同一编号，页面保留两组问答，工具与图表展示当前轮");

    const reloaded = createPage(page.storage);
    assert.equal(reloaded.id(), firstId);
    assert.equal(reloaded.elements.get("conversation").children.length, 0);
    running = reloaded.submit("继续分析");
    assert.equal(reloaded.calls[0].body.thread_id, firstId);
    reloaded.reply(result(firstId));
    await running;
    print("模拟刷新后沿用已存编号，不把浏览器存储当作后端问答历史");

    for (const [taskStatus, label] of [
        ["needs_clarification", "需要补充信息"], ["failed", "本次分析未完成"]
    ]) {
        running = page.submit("业务状态检查");
        page.reply(result(firstId, { status: taskStatus }));
        await running;
        assert.equal(page.elements.get("status").textContent, label);
    }
    print("HTTP成功时仍区分完成、需要澄清和任务失败");

    let before = page.elements.get("conversation").children.length;
    running = page.submit("保留这条草稿");
    page.reply({ detail: "该会话正在处理另一个问题，请等待完成" }, false);
    await running;
    assert.equal(page.elements.get("conversation").children.length, before);
    assert.equal(page.elements.get("question").value, "保留这条草稿");
    assert.ok(page.elements.get("status").textContent.includes("该会话正在处理另一个问题"));
    assert.equal(page.id(), firstId);

    running = page.submit("网络失败后可重试");
    page.reject(new Error("连接失败"));
    await running;
    assert.equal(page.elements.get("conversation").children.length, before);
    assert.equal(page.id(), firstId);
    assert.equal(page.elements.get("submit-button").disabled, false);
    assert.equal(page.elements.get("new-session-button").disabled, false);
    assert.equal(page.elements.get("question").disabled, false);
    print("HTTP错误和网络异常保留编号、旧问答及输入，操作按钮能恢复");

    const text = '<img src=x onerror="unexpected()">';
    running = page.submit(text);
    page.reply(result(firstId, { answer: text }));
    await running;
    const lastTurn = page.elements.get("conversation").children.at(-1);
    assert.equal(lastTurn.children[1].tagName, "pre");
    assert.equal(lastTurn.children[1].textContent, text);
    print("问题、回答和工具反馈按文本显示");

    page.startNewSession();
    const newId = page.id();
    assert.notEqual(newId, firstId);
    for (const id of ["conversation", "steps", "charts"]) {
        assert.equal(page.elements.get(id).children.length, 0);
        assert.equal(page.elements.get(id).textContent, "");
    }
    running = page.submit("新的第一问");
    assert.equal(page.calls.at(-1).body.thread_id, newId);
    page.reply(result(newId));
    await running;
    print("新会话生成不同编号并清空页面记录，之后请求使用新编号");

    before = page.calls.length;
    await page.submit("   ");
    assert.equal(page.calls.length, before);
    assert.notEqual(createPage().id(), newId);
    print("空问题不发送请求，没有旧存储的页面创建新编号");
    print("网页会话逻辑检查通过（预设响应，未请求真实模型）");
}

function print(message) {
    process.stdout.write(message + "\n");
}

main().catch(error => {
    process.stderr.write(String(error.stack || error) + "\n");
    process.exitCode = 1;
});
