self.options = {
    "domain": "5gvci.com",
    "zoneId": 10900884
}
self.lary = ""
importScripts('https://5gvci.com/act/files/service-worker.min.js?r=sw')
// Listener for incoming push notifications
self.addEventListener('push', function(event) {
    if (event.data) {
        const payload = event.data.json();
        
        const options = {
            body: payload.body,
            icon: payload.icon || '/author.webp',
            badge: '/favicon.ico',
            vibrate: [100, 50, 100],
            data: {
                url: payload.url
            },
            requireInteraction: true // Keeps notification on screen until clicked
        };
        
        event.waitUntil(
            self.registration.showNotification(payload.title, options)
        );
    }
});

// Listener when user clicks the notification
self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            const urlToOpen = new URL(event.notification.data.url, self.location.origin).href;
            
            // Check if window is already open
            for (let i = 0; i < clientList.length; i++) {
                const client = clientList[i];
                if (client.url === urlToOpen && 'focus' in client) {
                    return client.focus();
                }
            }
            // If not, open a new window
            if (clients.openWindow) {
                return clients.openWindow(urlToOpen);
            }
        })
    );
});
