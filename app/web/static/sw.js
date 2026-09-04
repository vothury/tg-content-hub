self.addEventListener('install', function(){ self.skipWaiting(); });
self.addEventListener('activate', function(e){ e.waitUntil(self.clients.claim()); });
self.addEventListener('push', function(e){
  var d={};try{d=e.data.json();}catch(err){}
  e.waitUntil(self.registration.showNotification(d.title||'TG Content Hub',{body:d.body||'',icon:'/static/icon.svg'}));
});
self.addEventListener('notificationclick', function(e){
  e.notification.close();
  e.waitUntil(self.clients.matchAll({type:'window', includeUncontrolled:true}).then(function(list){
    for (var i=0;i<list.length;i++){ if('focus' in list[i]) return list[i].focus(); }
    return self.clients.openWindow('/posts');
  }));
});