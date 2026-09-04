function ping(stage){
  return fetch('/api/webpush/ping',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({stage:stage})}).catch(function(){});
}

self.addEventListener('install', function(e){
  e.waitUntil(ping('installed'));
  self.skipWaiting();
});
self.addEventListener('activate', function(e){
  e.waitUntil(Promise.all([ping('activated'), self.clients.claim()]));
});

self.addEventListener('push', function(e){
  var d={};try{d=e.data.json();}catch(err){}
  var title=d.title||'TG Content Hub';
  var opts={body:d.body||'', icon:'/static/icon-512.png', badge:'/static/icon-512.png'};
  e.waitUntil(ping('received'));
  e.waitUntil(
    self.registration.showNotification(title, opts)
      .then(function(){ return ping('shown'); })
      .catch(function(){ return ping('show_failed'); })
  );
});

self.addEventListener('notificationclick', function(e){
  e.notification.close();
  e.waitUntil(self.clients.matchAll({type:'window', includeUncontrolled:true}).then(function(list){
    for (var i=0;i<list.length;i++){ if('focus' in list[i]) return list[i].focus(); }
    return self.clients.openWindow('/posts');
  }));
});