import SwiftUI

struct ContentView: View {
    @AppStorage("flightrisk_onboarding_complete") private var onboardingComplete = false

    var body: some View {
        if onboardingComplete {
            TabView {
                Text("Search")
                    .tabItem {
                        Label("Search", systemImage: "magnifyingglass")
                    }
                Text("Target")
                    .tabItem {
                        Label("Target", systemImage: "person.crop.circle")
                    }
                Text("Settings")
                    .tabItem {
                        Label("Settings", systemImage: "gearshape")
                    }
            }
        } else {
            Text("Onboarding Placeholder")
        }
    }
}
