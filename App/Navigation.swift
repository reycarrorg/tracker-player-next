import Foundation

struct NavigationState: Equatable {
    var section:String, worksheet:String, era:String, query:String, filter:String, kind:String
    var selection:Set<String>, inspector:Bool, loaded:Int, healthScope:String
}

struct NavigationTrail {
    private(set) var states:[NavigationState]=[]
    mutating func record(_ state:NavigationState) {
        guard states.last != state else{return}
        states.append(state)
        if states.count>100 {states.removeFirst(states.count-100)}
    }
    mutating func pop(current:NavigationState)->NavigationState? {
        while let state=states.popLast() {if state != current{return state}}
        return nil
    }
}

// WCAG relative luminance chooses the more legible foreground on the exact era fill.
func eraUsesDarkText(_ hex:String)->Bool {
    let n=UInt64(hex.trimmingCharacters(in:CharacterSet(charactersIn:"#")),radix:16) ?? 0x27262A
    func linear(_ channel:UInt64)->Double {
        let s=Double(channel)/255
        return s<=0.04045 ? s/12.92:pow((s+0.055)/1.055,2.4)
    }
    let luminance=0.2126*linear((n>>16)&255)+0.7152*linear((n>>8)&255)+0.0722*linear(n&255)
    return (luminance+0.05)/0.05 >= 1.05/(luminance+0.05)
}

// Track IDs come from the captured scope, after the backend duration filter.
func shuffleChoices(eligible:[String],current:String?,played:Set<String>,avoidRepeats:Bool,repeatAll:Bool)->(ids:[String],newCycle:Bool) {
    let unseen=eligible.filter{$0 != current && (!avoidRepeats || !played.contains($0))}
    if !unseen.isEmpty || !repeatAll{return (unseen,false)}
    let nextCycle=eligible.filter{$0 != current}
    return (nextCycle.isEmpty ? eligible:nextCycle,true)
}
