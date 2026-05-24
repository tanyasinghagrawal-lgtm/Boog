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

    // State Management
    const storageKey = `ai_chat_${currentSlug}`;
    let chatHistory = JSON.parse(localStorage.getItem(storageKey)) || [];
    const MAX_CHATS = 10;
    let isOpen = false;
    let isFullscreen = false;

    // 2. Inject CSS
    const style = document.createElement('style');
    style.innerHTML = `
        /* Widget Button */
        #ai-chat-btn {
            position: fixed; bottom: 20px; left: 20px; z-index: 9999;
            width: 60px; height: 60px; border-radius: 50%;
            background: linear-gradient(135deg, #10b981 0%, #047857 100%);
            box-shadow: 0 8px 32px rgba(16, 185, 129, 0.4);
            cursor: pointer; display: flex; align-items: center; justify-content: center;
            transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            border: 2px solid rgba(255,255,255,0.2);
        }
        #ai-chat-btn:hover { transform: scale(1.1); }
        #ai-chat-btn i { font-size: 28px; color: white; animation: pulse 2s infinite; }

        @keyframes pulse {
            0% { transform: scale(0.95); text-shadow: 0 0 0 rgba(255, 255, 255, 0.7); }
            70% { transform: scale(1.1); text-shadow: 0 0 15px rgba(255, 255, 255, 0); }
            100% { transform: scale(0.95); text-shadow: 0 0 0 rgba(255, 255, 255, 0); }
        }

        /* Glassmorphism Popup Window */
        #ai-chat-window {
            position: fixed; bottom: 90px; left: 20px; z-index: 9998;
            width: 380px; height: 550px; max-height: 85vh; max-width: calc(100vw - 40px);
            background: rgba(255, 255, 255, 0.65);
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

        /* Minimal Header */
        .ai-header {
            padding: 15px 20px; display: flex; justify-content: flex-end;
            align-items: center; border-bottom: 1px solid rgba(0,0,0,0.05);
            gap: 15px; background: rgba(255, 255, 255, 0.4);
        }
        .ai-header-icon {
            cursor: pointer; opacity: 0.6; transition: all 0.2s; font-size: 1.1rem; color: #1f2937;
        }
        .ai-header-icon:hover { opacity: 1; transform: scale(1.1); color: #047857;}

        /* Chat Area */
        .ai-body {
            flex: 1; overflow-y: auto; padding: 15px 20px; display: flex; flex-direction: column; gap: 16px;
            scroll-behavior: smooth;
        }
        .ai-body::-webkit-scrollbar { width: 6px; }
        .ai-body::-webkit-scrollbar-thumb { background: rgba(16, 185, 129, 0.5); border-radius: 3px; }

        /* Message Rows (Avatar + Message) */
        .msg-row { display: flex; gap: 10px; align-items: flex-end; width: 100%; }
        .msg-row.user { justify-content: flex-end; }
        .msg-row.ai { justify-content: flex-start; align-items: flex-start; }

        /* AI Avatar (Animated 3D Brain) */
        .ai-avatar {
            width: 34px; height: 34px; flex-shrink: 0; border-radius: 50%;
            background: radial-gradient(circle at 30% 30%, #10b981, #064e3b);
            box-shadow: 0 4px 10px rgba(16, 185, 129, 0.4), inset 0 2px 4px rgba(255,255,255,0.4);
            display: flex; align-items: center; justify-content: center;
            color: white; font-size: 15px;
            animation: brain-glow 3s infinite alternate;
        }
        @keyframes brain-glow {
            0% { box-shadow: 0 4px 10px rgba(16, 185, 129, 0.4), inset 0 2px 4px rgba(255,255,255,0.4); transform: translateY(0px); }
            100% { box-shadow: 0 4px 20px rgba(16, 185, 129, 0.8), inset 0 2px 4px rgba(255,255,255,0.6); transform: translateY(-2px); }
        }

        /* Messages */
        .msg { padding: 12px 16px; border-radius: 18px; font-size: 0.95rem; line-height: 1.5; max-width: 80%; word-wrap: break-word; }
        .msg.user { 
            background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; 
            border-bottom-right-radius: 4px; box-shadow: 0 4px 15px rgba(16, 185, 129, 0.2);
        }
        .msg.ai { 
            background: rgba(255, 255, 255, 0.8); color: #1f2937; 
            border-bottom-left-radius: 4px; border: 1px solid rgba(255,255,255,0.9);
            box-shadow: 0 4px 15px rgba(0,0,0,0.03); backdrop-filter: blur(10px);
        }
        
        /* Markdown formatting in AI messages (Fixed Gaps) */
        .msg.ai p { margin-bottom: 6px; margin-top: 0;}
        .msg.ai p:last-child { margin-bottom: 0; }
        .msg.ai ul, .msg.ai ol { margin-left: 0; padding-left: 20px; margin-top: 4px; margin-bottom: 8px; }
        .msg.ai li { margin-bottom: 3px; }
        .msg.ai li > ul { margin-top: 2px; margin-bottom: 2px; } /* Nested lists (e.g. India sub-points) */
        .msg.ai strong { color: #064e3b; font-weight: 700; }

        /* Suggestions */
        .ai-suggestions { display: flex; flex-direction: column; gap: 8px; margin-top: 5px; margin-left: 44px; }
        .suggestion-btn {
            background: rgba(255, 255, 255, 0.7); border: 1px solid rgba(16, 185, 129, 0.4); color: #047857;
            padding: 8px 14px; border-radius: 20px; font-size: 0.85rem; font-weight: 600;
            cursor: pointer; transition: all 0.2s; text-align: left; backdrop-filter: blur(5px);
        }
        .suggestion-btn:hover { background: #10b981; color: white; border-color: #10b981;}

        /* Input Area */
        .ai-footer { padding: 15px; border-top: 1px solid rgba(0,0,0,0.05); background: rgba(255,255,255,0.5); }
        .ai-input-group { display: flex; gap: 8px; background: rgba(255,255,255,0.7); border-radius: 25px; padding: 4px; border: 1px solid rgba(0,0,0,0.1);}
        .ai-input {
            flex: 1; padding: 10px 15px; border: none; background: transparent;
            outline: none; font-family: inherit; font-size: 0.95rem; color: #1f2937;
        }
        .ai-input::placeholder { color: #9ca3af; }
        .ai-send-btn {
            width: 40px; height: 40px; border-radius: 50%; background: #10b981;
            color: white; border: none; cursor: pointer; display: flex;
            align-items: center; justify-content: center; transition: all 0.2s;
            box-shadow: 0 4px 10px rgba(16, 185, 129, 0.3);
        }
        .ai-send-btn:hover { background: #059669; transform: scale(1.05);}

        /* Status & Limit */
        .ai-status { font-size: 0.75rem; color: #6b7280; text-align: center; margin-top: 10px; }
        .thinking { display: flex; gap: 4px; padding: 12px 16px; background: rgba(255,255,255,0.8); border-radius: 18px; border-bottom-left-radius: 4px;}
        .dot { width: 6px; height: 6px; background: #10b981; border-radius: 50%; animation: blink 1.4s infinite both; }
        .dot:nth-child(2) { animation-delay: 0.2s; }
        .dot:nth-child(3) { animation-delay: 0.4s; }
        @keyframes blink { 0%, 80%, 100% { opacity: 0; } 40% { opacity: 1; } }

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

    // 3. Build UI (Minimal Header with Fullscreen Icon)
    const widgetObj = document.createElement('div');
    widgetObj.innerHTML = `
        <div id="ai-chat-window">
            <div class="ai-header">
                <i class="fas fa-expand ai-header-icon" id="ai-fullscreen-btn" title="Toggle Fullscreen"></i>
                <i class="fas fa-times ai-header-icon" id="ai-close-btn" title="Close"></i>
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
            <i class="fas fa-brain"></i>
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

    // 4. Load Chat History
    function renderChat() {
        bodyEl.innerHTML = '';
        if (chatHistory.length === 0) {
            // Initial AI Greeting Wrapper
            const wrap = document.createElement('div');
            wrap.className = 'msg-row ai';
            wrap.innerHTML = `
                <div class="ai-avatar"><i class="fas fa-brain"></i></div>
                <div class="msg ai">Hi! I am the AI guide for this article. How can I help you today?</div>
            `;
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
            // Fix: set isMarkdown to true so refresh parses correctly
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

    // Updated appendMessage to include Avatar structure
    function appendMessage(role, text, isMarkdown = true) {
        const rowDiv = document.createElement('div');
        rowDiv.className = `msg-row ${role}`;
        
        let avatarHtml = '';
        if(role === 'ai') {
            avatarHtml = `<div class="ai-avatar"><i class="fas fa-brain"></i></div>`;
        }

        const msgDiv = document.createElement('div');
        msgDiv.className = `msg ${role}`;
        
        if (role === 'ai' && isMarkdown && typeof marked !== 'undefined') {
            msgDiv.innerHTML = marked.parse(text);
        } else {
            msgDiv.textContent = text;
        }
        
        // Assemble User vs AI layout
        if(role === 'ai') {
            rowDiv.innerHTML = avatarHtml;
            rowDiv.appendChild(msgDiv);
        } else {
            rowDiv.appendChild(msgDiv);
        }

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

        // Add Thinking Animation with Avatar layout
        const thinkRow = document.createElement('div');
        thinkRow.className = 'msg-row ai';
        thinkRow.innerHTML = `
            <div class="ai-avatar"><i class="fas fa-brain"></i></div>
            <div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
        `;
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
            fullBtn.classList.remove('fa-expand');
            fullBtn.classList.add('fa-compress');
        } else {
            windowEl.classList.remove('fullscreen');
            fullBtn.classList.remove('fa-compress');
            fullBtn.classList.add('fa-expand');
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
