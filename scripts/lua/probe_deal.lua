-- Run in the DiplomacyDealView Lua state while a deal screen is open.
local s = ""
for k, v in pairs(DealProposalAction) do s = s .. k .. "=" .. tostring(v) .. " " end
print("ENUM DealProposalAction " .. s)

s = ""
for k, v in pairs(DealDirection) do s = s .. k .. "=" .. tostring(v) .. " " end
print("ENUM DealDirection " .. s)

local me = g_LocalPlayer and g_LocalPlayer:GetID() or -1
print("VIEW local=" .. tostring(me)
  .. " other=" .. tostring(ms_OtherPlayerID)
  .. " initiatedBy=" .. tostring(ms_InitiatedByPlayerID)
  .. " isDemand=" .. tostring(ms_bIsDemand)
  .. " autoPropose=" .. tostring(IsAutoPropose and IsAutoPropose()))

if me >= 0 and ms_OtherPlayerID and ms_OtherPlayerID >= 0 then
  print("OTHER Players:IsHuman=" .. tostring(Players[ms_OtherPlayerID]:IsHuman())
    .. " Config:IsHuman=" .. tostring(PlayerConfigurations[ms_OtherPlayerID]:IsHuman())
    .. " hasPending=" .. tostring(DealManager.HasPendingDeal(me, ms_OtherPlayerID)))

  local pDeal = DealManager.GetWorkingDeal(DealDirection.OUTGOING, me, ms_OtherPlayerID)
  if pDeal == nil then
    print("DEAL nil")
  else
    print("DEAL items=" .. tostring(pDeal:GetItemCount())
      .. " valid=" .. tostring(pDeal:IsValid())
      .. " gift=" .. tostring(pDeal:IsGift()))
    for i, pItem in ipairs(pDeal:FindItemsByType(DealItemTypes.GOLD) or {}) do
      print("  GOLD[" .. i .. "] from=" .. tostring(pItem:GetFromPlayerID())
        .. " amount=" .. tostring(pItem:GetAmount())
        .. " duration=" .. tostring(pItem:GetDuration()))
    end
  end
end
