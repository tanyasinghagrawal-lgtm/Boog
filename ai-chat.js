(function () {
    // 1. Domain Protection (Bahar kaam nahi karega)
    const allowedDomains = ["pranavblog.online", "localhost", "127.0.0.1"];
    if (!allowedDomains.some(domain => window.location.hostname.includes(domain))) {
        console.warn("AI Chat Widget is not authorized for this domain.");
        return;
    }

    // Get current blog slug
    const currentSlug = window.location.pathname.replace(/^\/|\/$/g, '');
    if (!currentSlug || currentSlug === 'home') return; // Don't show on home page

    // State Management & 24hr History Expiry
    const storageKey = `ai_chat_${currentSlug}`;
    const timeKey = `ai_chat_time_${currentSlug}`;
    
    // Clear history if older than 24 hours
    const lastLoad = localStorage.getItem(timeKey);
    const now = Date.now();
    if (lastLoad && (now - parseInt(lastLoad)) > 2400 * 60 * 60 * 1000) {
        localStorage.removeItem(storageKey);
        localStorage.setItem(timeKey, now);
    } else if (!lastLoad) {
        localStorage.setItem(timeKey, now);
    }

    let chatHistory = JSON.parse(localStorage.getItem(storageKey)) || [];
    const MAX_CHATS = 10;
    let isOpen = false;
    let isFullscreen = false;

    // 2. Inject CSS
    const style = document.createElement('style');
    style.innerHTML = `
        /* Widget Button - Yellowish Pink Gradient */
        #ai-chat-btn {
            position: fixed; bottom: 20px; left: 20px; z-index: 9999;
            width: 60px; height: 60px; border-radius: 50%;
            background: linear-gradient(135deg, #fcb69f 0%, #ffecd2 100%);
            box-shadow: 0 8px 32px rgba(252, 182, 159, 0.5);
            cursor: pointer; display: flex; align-items: center; justify-content: center;
            transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            border: 2px solid rgba(255,255,255,0.4);
        }
        #ai-chat-btn:hover { transform: scale(1.1); }
        #ai-chat-btn i { font-size: 28px; color: #d9480f; animation: pulse 2s infinite; }

        @keyframes pulse {
            0% { transform: scale(0.95); text-shadow: 0 0 0 rgba(255, 255, 255, 0.7); }
            70% { transform: scale(1.1); text-shadow: 0 0 15px rgba(255, 255, 255, 0); }
            100% { transform: scale(0.95); text-shadow: 0 0 0 rgba(255, 255, 255, 0); }
        }

        /* Glassmorphism Popup Window - Light Yellowish Pink */
        #ai-chat-window {
            position: fixed; bottom: 90px; left: 20px; z-index: 9998;
            width: 380px; height: 550px; max-height: 85vh; max-width: calc(100vw - 40px);
            background: linear-gradient(135deg, rgba(255, 240, 245, 0.75), rgba(255, 250, 205, 0.75));
            backdrop-filter: blur(25px); -webkit-backdrop-filter: blur(25px);
            border: 1px solid rgba(255, 255, 255, 0.8);
            border-radius: 24px; display: flex; flex-direction: column;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1), inset 0 0 0 1px rgba(255,255,255,0.5);
            opacity: 0; pointer-events: none; transform: translateY(20px) scale(0.95);
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            font-family: 'Quicksand', sans-serif; overflow: hidden;
        }
        
        #ai-chat-window.active { opacity: 1; pointer-events: auto; transform: translateY(0) scale(1); }
        
        /* Fullscreen Mode */
        #ai-chat-window.fullscreen {
            width: 100vw; height: 100vh; max-height: 100vh; max-width: 100vw;
            bottom: 0; left: 0; border-radius: 0; border: none;
        }

        /* Floating Transparent Controls (Replaced Header) */
        .floating-controls {
            position: absolute; top: 12px; right: 15px; z-index: 100;
            display: flex; gap: 10px;
        }
        .floating-btn {
            width: 32px; height: 32px; border-radius: 50%;
            background: rgba(255, 255, 255, 0.3); border: 1px solid rgba(255, 255, 255, 0.5);
            display: flex; align-items: center; justify-content: center;
            cursor: pointer; opacity: 0.7; transition: all 0.2s; color: #4a4a4a;
            backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px);
        }
        .floating-btn:hover {
            opacity: 1; background: rgba(255, 255, 255, 0.8);
            transform: scale(1.05); color: #d9480f;
        }

        /* Chat Area */
        .ai-body {
            flex: 1; overflow-y: auto; padding: 50px 20px 15px 20px; /* Top padding added for floating buttons */
            display: flex; flex-direction: column; gap: 16px; scroll-behavior: smooth;
        }
        .ai-body::-webkit-scrollbar { width: 6px; }
        .ai-body::-webkit-scrollbar-thumb { background: rgba(252, 182, 159, 0.6); border-radius: 3px; }

        /* Message Rows */
        .msg-row { display: flex; gap: 10px; align-items: flex-end; width: 100%; }
        .msg-row.user { justify-content: flex-end; }
        .msg-row.ai { justify-content: flex-start; align-items: flex-start; }

        /* Messages */
        .msg { padding: 12px 16px; border-radius: 18px; font-size: 0.95rem; line-height: 1.5; max-width: 85%; word-wrap: break-word; }
        .msg.user { 
            background: linear-gradient(135deg, #fcb69f 0%, #ffecd2 100%); color: #4a2c11; 
            border-bottom-right-radius: 4px; box-shadow: 0 4px 15px rgba(252, 182, 159, 0.2);
            font-weight: 500;
        }
        .msg.ai { 
            background: rgba(255, 255, 255, 0.85); color: #374151; 
            border-bottom-left-radius: 4px; border: 1px solid rgba(255,255,255,0.9);
            box-shadow: 0 4px 15px rgba(0,0,0,0.04); backdrop-filter: blur(10px);
            width: 100%; /* Take full width available */
        }
        
        /* Markdown formatting in AI messages */
        .msg.ai p { margin-bottom: 6px; margin-top: 0;}
        .msg.ai p:last-child { margin-bottom: 0; }
        .msg.ai ul, .msg.ai ol { margin-left: 0; padding-left: 20px; margin-top: 4px; margin-bottom: 8px; }
        .msg.ai li { margin-bottom: 3px; }
        .msg.ai li > ul { margin-top: 2px; margin-bottom: 2px; } 
        .msg.ai strong { color: #d9480f; font-weight: 700; }

        /* Suggestions */
        .ai-suggestions { display: flex; flex-direction: column; gap: 8px; margin-top: 5px; }
        .suggestion-btn {
            background: rgba(255, 255, 255, 0.6); border: 1px solid rgba(252, 182, 159, 0.6); color: #d9480f;
            padding: 8px 14px; border-radius: 20px; font-size: 0.85rem; font-weight: 600;
            cursor: pointer; transition: all 0.2s; text-align: left; backdrop-filter: blur(5px);
        }
        .suggestion-btn:hover { background: #fcb69f; color: white; border-color: #fcb69f;}

        /* Input Area */
        .ai-footer { padding: 15px; border-top: 1px solid rgba(255,255,255,0.4); background: rgba(255,255,255,0.4); }
        .ai-input-group { display: flex; gap: 8px; background: rgba(255,255,255,0.8); border-radius: 25px; padding: 4px; border: 1px solid rgba(0,0,0,0.05); box-shadow: inset 0 2px 5px rgba(0,0,0,0.02);}
        .ai-input {
            flex: 1; padding: 10px 15px; border: none; background: transparent;
            outline: none; font-family: inherit; font-size: 0.95rem; color: #1f2937;
        }
        .ai-input::placeholder { color: #9ca3af; }
        .ai-send-btn {
            width: 40px; height: 40px; border-radius: 50%; background: linear-gradient(135deg, #fcb69f 0%, #ffecd2 100%);
            color: #d9480f; border: none; cursor: pointer; display: flex;
            align-items: center; justify-content: center; transition: all 0.2s;
            box-shadow: 0 4px 10px rgba(252, 182, 159, 0.3);
        }
        .ai-send-btn:hover { transform: scale(1.05); color: white; background: #d9480f;}

        /* Status & Thinking Animation */
        .ai-status { font-size: 0.75rem; color: #6b7280; text-align: center; margin-top: 10px; }
        .thinking { display: flex; gap: 5px; padding: 14px 18px; background: rgba(255,255,255,0.85); border-radius: 18px; border-bottom-left-radius: 4px; width: max-content; box-shadow: 0 4px 15px rgba(0,0,0,0.04);}
        .dot { width: 7px; height: 7px; background: #fcb69f; border-radius: 50%; animation: blink 1.4s infinite both; }
        .dot:nth-child(2) { animation-delay: 0.2s; }
        .dot:nth-child(3) { animation-delay: 0.4s; }
        @keyframes blink { 0%, 80%, 100% { opacity: 0.2; transform: scale(0.8); } 40% { opacity: 1; transform: scale(1.2); } }

        /* Limit Reached UI */
        .limit-reached { text-align: center; color: #dc2626; font-size: 0.85rem; font-weight: bold; margin-bottom: 10px;}
        .delete-session-btn {
            width: 100%; background: rgba(254, 226, 226, 0.8); color: #b91c1c; border: 1px solid #f87171;
            padding: 10px; border-radius: 15px; font-weight: bold; cursor: pointer;
            transition: all 0.2s; backdrop-filter: blur(5px);
        }
        .delete-session-btn:hover { background: #fecaca; }

        @media (max-width: 480px) {
            #ai-chat-window { bottom: 0; left: 0; width: 100vw; height: 80vh; max-width: 100vw; border-radius: 24px 24px 0 0; }
            #ai-chat-window.fullscreen { height: 100vh; border-radius: 0; }
            #ai-chat-btn { bottom: 20px; right: 20px; left: auto; }
        }
    `;
    document.head.appendChild(style);

    // 3. Build UI (No header, Floating buttons inside window)
    const widgetObj = document.createElement('div');
    widgetObj.innerHTML = `
        <div id="ai-chat-window">
            <div class="floating-controls">
                <div class="floating-btn" id="ai-fullscreen-btn" title="Toggle Fullscreen"><i class="fas fa-expand"></i></div>
                <div class="floating-btn" id="ai-close-btn" title="Close"><i class="fas fa-times"></i></div>
            </div>
            <div class="ai-body" id="ai-chat-body"></div>
            <div class="ai-footer" id="ai-chat-footer">
                <div class="ai-input-group">
                    <input type="text" id="ai-chat-input" class="ai-input" placeholder="Ask about this article..." />
                    <button id="ai-chat-send" class="ai-send-btn"><i class="fas fa-paper-plane"></i></button>
                </div>
                <div class="ai-status" id="ai-chat-status">AI responses may contain errors</div>
            </div>
        </div>
        <div id="ai-chat-btn">
            <i class="fas fa-magic"></i>
        </div>
    `;
    document.body.appendChild(widgetObj);

    // Elements
    const btn = document.getElementById('ai-chat-btn');
    const windowEl = document.getElementById('ai-chat-window');
    const closeBtn = document.getElementById('ai-close-btn');
    const fullBtn = document.getElementById('ai-fullscreen-btn');
    const bodyEl = document.getElementById('ai-chat-body');
    const footerEl = document.getElementById('ai-chat-footer');
    const inputEl = document.getElementById('ai-chat-input');
    const sendBtn = document.getElementById('ai-chat-send');
    const fullIcon = fullBtn.querySelector('i');

    // 4. Load Chat History
    function renderChat() {
        bodyEl.innerHTML = '';
        if (chatHistory.length === 0) {
            // Initial AI Greeting Wrapper (No Avatar)
            const wrap = document.createElement('div');
            wrap.className = 'msg-row ai';
            wrap.innerHTML = `<div class="msg ai">Hi! I am the AI guide for this article. How can I help you today?</div>`;
            bodyEl.appendChild(wrap);
            
            // Suggestions
            const suggDiv = document.createElement('div');
            suggDiv.className = 'ai-suggestions';
            suggDiv.id = 'ai-suggestions';
            suggDiv.innerHTML = `
                <button class="suggestion-btn" onclick="sendQuery('Summarize this article')">⚡ Summarize this article</button>
                <button class="suggestion-btn" onclick="sendQuery('Explain all important facts of this article')">💡 Explain all important facts</button>
            `;
            bodyEl.appendChild(suggDiv);
        } else {
            chatHistory.forEach(msg => {
                appendMessage(msg.role, msg.content, true);
            });
        }
        checkLimit();
        scrollToBottom();
    }

    function checkLimit() {
        if (chatHistory.length >= MAX_CHATS * 2) {
            footerEl.innerHTML = `
                <div class="limit-reached">Session Limit Reached (10/10)</div>
                <button class="delete-session-btn" onclick="deleteSession()"><i class="fas fa-trash-alt"></i> Delete Session & Restart</button>
            `;
        }
    }

    window.deleteSession = function() {
        localStorage.removeItem(storageKey);
        localStorage.setItem(timeKey, Date.now()); // Reset time
        chatHistory = [];
        footerEl.innerHTML = `
            <div class="ai-input-group">
                <input type="text" id="ai-chat-input" class="ai-input" placeholder="Ask about this article..." />
                <button id="ai-chat-send" class="ai-send-btn"><i class="fas fa-paper-plane"></i></button>
            </div>
            <div class="ai-status" id="ai-chat-status">AI responses may contain errors</div>
        `;
        document.getElementById('ai-chat-send').addEventListener('click', () => {
            const input = document.getElementById('ai-chat-input');
            if (input.value.trim()) sendQuery(input.value.trim());
        });
        document.getElementById('ai-chat-input').addEventListener('keypress', (e) => {
            if (e.key === 'Enter' && e.target.value.trim()) sendQuery(e.target.value.trim());
        });
        renderChat();
    }

    // Updated appendMessage (Removed Avatar layout for wider space)
    function appendMessage(role, text, isMarkdown = true) {
        const rowDiv = document.createElement('div');
        rowDiv.className = `msg-row ${role}`;
        
        const msgDiv = document.createElement('div');
        msgDiv.className = `msg ${role}`;
        
        if (role === 'ai' && isMarkdown && typeof marked !== 'undefined') {
            msgDiv.innerHTML = marked.parse(text);
        } else {
            msgDiv.textContent = text;
        }
        
        rowDiv.appendChild(msgDiv);
        bodyEl.appendChild(rowDiv);
        scrollToBottom();
        return msgDiv; 
    }

    function scrollToBottom() {
        bodyEl.scrollTop = bodyEl.scrollHeight;
    }

    // 5. Send Query & Stream Logic
    window.sendQuery = async function(text) {
        if (!text) return;
        
        const sugg = document.getElementById('ai-suggestions');
        if (sugg) sugg.remove();

        const inputField = document.getElementById('ai-chat-input');
        if(inputField) inputField.value = '';
        
        appendMessage('user', text);
        const requestHistory = [...chatHistory]; 
        chatHistory.push({ role: 'user', content: text });
        localStorage.setItem(storageKey, JSON.stringify(chatHistory));
        localStorage.setItem(timeKey, Date.now()); // Update Activity time

        // Add Thinking Animation (No Avatar)
        const thinkRow = document.createElement('div');
        thinkRow.className = 'msg-row ai';
        thinkRow.innerHTML = `<div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
        bodyEl.appendChild(thinkRow);
        scrollToBottom();

        if(inputField) inputField.disabled = true;

        try {
            const response = await fetch('/ai-chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ slug: currentSlug, query: text, history: requestHistory })
            });

            thinkRow.remove();
            if (!response.ok) throw new Error("Network response was not ok");

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            
            const aiMsgDiv = appendMessage('ai', '', false);
            let aiFullResponse = "";

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                
                const chunkString = decoder.decode(value, { stream: true });
                const lines = chunkString.split('\n');
                
                for (let line of lines) {
                    if (line.startsWith('data: ')) {
                        try {
                            const data = JSON.parse(line.substring(6));
                            if (data.chunk) {
                                aiFullResponse += data.chunk;
                                if (typeof marked !== 'undefined') {
                                    aiMsgDiv.innerHTML = marked.parse(aiFullResponse);
                                } else {
                                    aiMsgDiv.textContent = aiFullResponse;
                                }
                                scrollToBottom();
                            }
                        } catch (e) { }
                    }
                }
            }

            chatHistory.push({ role: 'ai', content: aiFullResponse });
            localStorage.setItem(storageKey, JSON.stringify(chatHistory));

        } catch (error) {
            thinkRow.remove();
            appendMessage('ai', 'Oops! I lost connection to the server. Please try again.');
        } finally {
            if(inputField) {
                inputField.disabled = false;
                inputField.focus();
            }
            checkLimit();
        }
    };

    // 6. Event Listeners
    btn.addEventListener('click', () => {
        isOpen = !isOpen;
        if (isOpen) {
            windowEl.classList.add('active');
            btn.style.transform = "scale(0)";
        }
    });

    closeBtn.addEventListener('click', () => {
        isOpen = false;
        windowEl.classList.remove('active');
        btn.style.transform = "scale(1)";
    });

    // Fullscreen Toggle Logic
    fullBtn.addEventListener('click', () => {
        isFullscreen = !isFullscreen;
        if(isFullscreen) {
            windowEl.classList.add('fullscreen');
            fullIcon.classList.remove('fa-expand');
            fullIcon.classList.add('fa-compress');
        } else {
            windowEl.classList.remove('fullscreen');
            fullIcon.classList.remove('fa-compress');
            fullIcon.classList.add('fa-expand');
        }
    });

    sendBtn.addEventListener('click', () => {
        sendQuery(inputEl.value.trim());
    });

    inputEl.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') sendQuery(inputEl.value.trim());
    });

    // Init
    renderChat();
})();
