// One shared object coordinates expiring leases across per-connection objects.
const LEASE_MS = 300000;
export const SESSION_MS = 3600000;
function limit(value, fallback, maximum) {
  const number=Number(value ?? fallback);
  if (!Number.isSafeInteger(number) || number<1 || number>maximum)
    throw new Error("Invalid subscription connection limit");
  return number;
}
export async function handleAdmission(ctx,env,payload) {
  const {action,id,client}=payload;
  if (!["reserve","renew","release"].includes(action) || typeof id!=="string" || !id || id.length>64 ||
      ["__proto__","constructor","prototype"].includes(id))
    return Response.json({error:"Invalid admission request"},{status:400});
  const maximum=limit(env.CATALOG_SUBSCRIPTION_MAX_CONNECTIONS,256,512);
  const perClient=limit(env.CATALOG_SUBSCRIPTION_CONNECTIONS_PER_IP,16,512);
  return ctx.storage.transaction(async storage=>{
    const now=Date.now();
    const leases=(await storage.get("leases") ?? []).filter(row=>row.expires>now);
    const found=leases.find(row=>row.id===id);
    if (action==="release") {
      await storage.put("leases",leases.filter(row=>row.id!==id));
      return Response.json({ok:true});
    }
    if (action==="renew") {
      if (!found || now-found.opened>=SESSION_MS)
        return Response.json({error:"Subscription lease expired; reconnect"},{status:429});
      found.expires=Math.min(now+LEASE_MS,found.opened+SESSION_MS);
    } else if (!found) {
      if (typeof client!=="string" || !/^[a-f0-9]{64}$/.test(client))
        return Response.json({error:"Invalid client identity"},{status:400});
      if (leases.length>=maximum || leases.filter(row=>row.client===client).length>=perClient)
        return Response.json({error:"Subscription capacity reached; retry later"},{status:429,
          headers:{"Retry-After":"60"}});
      leases.push({id,client,opened:now,expires:now+LEASE_MS});
    }
    await storage.put("leases",leases);
    return Response.json({ok:true});
  });
}
export function admission(env,action,id,client) {
  return env.SUBSCRIPTIONS.get(env.SUBSCRIPTIONS.idFromName("admission")).fetch(
    "https://subscription.internal/admission",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({action,id,client})});
}
