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
            width: 350px; height: 500px; max-height: 80vh; max-width: calc(100vw - 40px);
            background: rgba(255, 255, 255, 0.85);
            backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(16, 185, 129, 0.3);
            border-radius: 20px; display: flex; flex-direction: column;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.15);
            opacity: 0; pointer-events: none; transform: translateY(20px) scale(0.95);
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            font-family: 'Quicksand', sans-serif; overflow: hidden;
        }
        #ai-chat-window.active { opacity: 1; pointer-events: auto; transform: translateY(0) scale(1); }

        /* Header */
        .ai-header {
            background: linear-gradient(135deg, #064e3b 0%, #047857 100%);
            color: white; padding: 15px 20px; display: flex; justify-content: space-between;
            align-items: center; border-radius: 20px 20px 0 0;
        }
        .ai-header-title { font-weight: 700; font-size: 1.1rem; display: flex; align-items: center; gap: 8px;}
        .ai-close { cursor: pointer; opacity: 0.8; transition: opacity 0.2s; }
        .ai-close:hover { opacity: 1; }

        /* Chat Area */
        .ai-body {
            flex: 1; overflow-y: auto; padding: 15px; display: flex; flex-direction: column; gap: 12px;
            scroll-behavior: smooth;
        }
        .ai-body::-webkit-scrollbar { width: 6px; }
        .ai-body::-webkit-scrollbar-thumb { background: #10b981; border-radius: 3px; }

        /* Messages */
        .msg { padding: 10px 15px; border-radius: 15px; font-size: 0.9rem; line-height: 1.4; max-width: 85%; word-wrap: break-word;}
        .msg.user { background: #dcfce7; color: #065f46; align-self: flex-end; border-bottom-right-radius: 5px; }
        .msg.ai { background: white; color: #1f2937; align-self: flex-start; border-bottom-left-radius: 5px; border: 1px solid #e5e7eb; box-shadow: 0 2px 5px rgba(0,0,0,0.02);}
        
        /* Markdown formatting in AI messages */
        .msg.ai p { margin-bottom: 8px; }
        .msg.ai p:last-child { margin-bottom: 0; }
        .msg.ai ul, .msg.ai ol { margin-left: 20px; margin-bottom: 8px; }
        .msg.ai strong { color: #064e3b; }

        /* Suggestions */
        .ai-suggestions { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
        .suggestion-btn {
            background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #047857;
            padding: 8px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 600;
            cursor: pointer; transition: all 0.2s; text-align: left;
        }
        .suggestion-btn:hover { background: #10b981; color: white; }

        /* Input Area */
        .ai-footer { padding: 15px; border-top: 1px solid rgba(0,0,0,0.05); background: rgba(255,255,255,0.9); }
        .ai-input-group { display: flex; gap: 8px; }
        .ai-input {
            flex: 1; padding: 12px 15px; border-radius: 25px; border: 1px solid #d1d5db;
            outline: none; font-family: inherit; font-size: 0.9rem; background: #f9fafb;
            transition: border-color 0.2s;
        }
        .ai-input:focus { border-color: #10b981; }
        .ai-send-btn {
            width: 42px; height: 42px; border-radius: 50%; background: #10b981;
            color: white; border: none; cursor: pointer; display: flex;
            align-items: center; justify-content: center; transition: background 0.2s;
        }
        .ai-send-btn:hover { background: #059669; }

        /* Status & Limit */
        .ai-status { font-size: 0.75rem; color: #6b7280; text-align: center; margin-top: 8px; }
        .thinking { display: flex; gap: 4px; padding: 10px 15px; background: white; align-self: flex-start; border-radius: 15px; border: 1px solid #e5e7eb;}
        .dot { width: 6px; height: 6px; background: #10b981; border-radius: 50%; animation: blink 1.4s infinite both; }
        .dot:nth-child(2) { animation-delay: 0.2s; }
        .dot:nth-child(3) { animation-delay: 0.4s; }
        @keyframes blink { 0%, 80%, 100% { opacity: 0; } 40% { opacity: 1; } }

        /* Limit Reached UI */
        .limit-reached { text-align: center; color: #dc2626; font-size: 0.85rem; font-weight: bold; margin-bottom: 10px;}
        .delete-session-btn {
            width: 100%; background: #fee2e2; color: #b91c1c; border: 1px solid #f87171;
            padding: 10px; border-radius: 15px; font-weight: bold; cursor: pointer;
            transition: all 0.2s;
        }
        .delete-session-btn:hover { background: #fecaca; }

        @media (max-width: 480px) {
            #ai-chat-window { bottom: 0; left: 0; width: 100vw; height: 70vh; max-width: 100vw; border-radius: 20px 20px 0 0; border: none; border-top: 1px solid rgba(16, 185, 129, 0.3);}
            #ai-chat-btn { bottom: 20px; right: 20px; left: auto; }
        }
    `;
    document.head.appendChild(style);

    // 3. Build UI
    const widgetObj = document.createElement('div');
    widgetObj.innerHTML = `
        <div id="ai-chat-window">
            <div class="ai-header">
                <div class="ai-header-title"><i class="fas fa-brain"></i> Blog AI Guide</div>
                <div class="ai-close"><i class="fas fa-times"></i></div>
            </div>
            <div class="ai-body" id="ai-chat-body"></div>
            <div class="ai-footer" id="ai-chat-footer">
                <div class="ai-input-group">
                    <input type="text" id="ai-chat-input" class="ai-input" placeholder="Ask about this article..." />
                    <button id="ai-chat-send" class="ai-send-btn"><i class="fas fa-paper-plane"></i></button>
                </div>
                <div class="ai-status" id="ai-chat-status">Powered by Gemini AI</div>
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
    const closeBtn = document.querySelector('.ai-close');
    const bodyEl = document.getElementById('ai-chat-body');
    const footerEl = document.getElementById('ai-chat-footer');
    const inputEl = document.getElementById('ai-chat-input');
    const sendBtn = document.getElementById('ai-chat-send');
    const statusEl = document.getElementById('ai-chat-status');

    // 4. Load Chat History
    function renderChat() {
        bodyEl.innerHTML = '';
        if (chatHistory.length === 0) {
            bodyEl.innerHTML = `
                <div class="msg ai">Hi! I am the AI guide for this article. How can I help you today?</div>
                <div class="ai-suggestions" id="ai-suggestions">
                    <button class="suggestion-btn" onclick="sendQuery('Summarize this article')">⚡ Summarize this article</button>
                    <button class="suggestion-btn" onclick="sendQuery('Explain all important facts of this article')">💡 Explain all important facts</button>
                </div>
            `;
        } else {
            chatHistory.forEach(msg => {
                appendMessage(msg.role, msg.content, false);
            });
        }
        checkLimit();
        scrollToBottom();
    }

    function checkLimit() {
        // Pairs of Q&A. 10 chats = 20 messages.
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
        // Reset Footer UI
        footerEl.innerHTML = `
            <div class="ai-input-group">
                <input type="text" id="ai-chat-input" class="ai-input" placeholder="Ask about this article..." />
                <button id="ai-chat-send" class="ai-send-btn"><i class="fas fa-paper-plane"></i></button>
            </div>
            <div class="ai-status" id="ai-chat-status">Powered by Gemini AI</div>
        `;
        // Re-attach listeners
        document.getElementById('ai-chat-send').addEventListener('click', () => {
            const input = document.getElementById('ai-chat-input');
            if (input.value.trim()) sendQuery(input.value.trim());
        });
        document.getElementById('ai-chat-input').addEventListener('keypress', (e) => {
            if (e.key === 'Enter' && e.target.value.trim()) sendQuery(e.target.value.trim());
        });
        renderChat();
    }

    function appendMessage(role, text, isMarkdown = true) {
        const msgDiv = document.createElement('div');
        msgDiv.className = `msg ${role}`;
        
        // Parse markdown if it's AI response and marked is available
        if (role === 'ai' && isMarkdown && typeof marked !== 'undefined') {
            msgDiv.innerHTML = marked.parse(text);
        } else {
            msgDiv.textContent = text;
        }
        
        bodyEl.appendChild(msgDiv);
        scrollToBottom();
        return msgDiv; // Return reference for streaming updates
    }

    function scrollToBottom() {
        bodyEl.scrollTop = bodyEl.scrollHeight;
    }

    // 5. Send Query & Stream Logic
    window.sendQuery = async function(text) {
        if (!text) return;
        
        // Remove suggestions if they exist
        const sugg = document.getElementById('ai-suggestions');
        if (sugg) sugg.remove();

        const inputField = document.getElementById('ai-chat-input');
        if(inputField) inputField.value = '';
        
        // Add User Message
        appendMessage('user', text);
        const requestHistory = [...chatHistory]; // Send current history before appending new one
        chatHistory.push({ role: 'user', content: text });
        localStorage.setItem(storageKey, JSON.stringify(chatHistory));

        // Add Thinking Animation
        const thinkingDiv = document.createElement('div');
        thinkingDiv.className = 'thinking';
        thinkingDiv.innerHTML = '<div class="dot"></div><div class="dot"></div><div class="dot"></div>';
        bodyEl.appendChild(thinkingDiv);
        scrollToBottom();

        if(inputField) inputField.disabled = true;

        try {
            // Using SSE (Fetch API stream)
            const response = await fetch('/ai-chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    slug: currentSlug,
                    query: text,
                    history: requestHistory
                })
            });

            thinkingDiv.remove();

            if (!response.ok) throw new Error("Network response was not ok");

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            
            // Create an empty AI message div
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
                                // Update HTML dynamically using marked if available
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

            // Save AI response to history
            chatHistory.push({ role: 'ai', content: aiFullResponse });
            localStorage.setItem(storageKey, JSON.stringify(chatHistory));

        } catch (error) {
            thinkingDiv.remove();
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
            btn.style.transform = "scale(0)"; // Hide button when open
        }
    });

    closeBtn.addEventListener('click', () => {
        isOpen = false;
        windowEl.classList.remove('active');
        btn.style.transform = "scale(1)"; // Show button
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
