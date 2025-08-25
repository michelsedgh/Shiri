package main

import (
    "fmt"
    "image/color"
    "log"
    "os"

    "fyne.io/fyne/v2"
    "fyne.io/fyne/v2/app"
    "fyne.io/fyne/v2/container"
    "fyne.io/fyne/v2/data/binding"
    "fyne.io/fyne/v2/theme"
    "fyne.io/fyne/v2/widget"

    cfg "shiri-linux/internal/config"
    "shiri-linux/internal/engine"
    "shiri-linux/internal/netifaces"
    "shiri-linux/internal/netsetup"
    "shiri-linux/internal/rooms"
    "shiri-linux/internal/ssdp"
    "shiri-linux/internal/upnp"
    "shiri-linux/internal/systemcheck"
)

func main() {
    a := app.NewWithID("shiri-linux")
    a.Settings().SetTheme(theme.DarkTheme())
    w := a.NewWindow("Shiri Linux")
    w.Resize(fyne.NewSize(900, 600))

    // Ensure config dir exists and load config
    appConfig, err := cfg.Load()
    if err != nil {
        log.Printf("failed to load config: %v", err)
    }

    // Status labels
    engineLabel := widget.NewLabel("Engine: detecting…")
    nicsLabel := widget.NewLabel("NICs: …")

    // Buttons
    detectBtn := widget.NewButton("Detect Engine", func() {
        eng := engine.Detect()
        engineLabel.SetText(fmt.Sprintf("Engine: %s", eng.String()))
    })

    refreshNicsBtn := widget.NewButton("Refresh NICs", func() {
        ifs := netifaces.List()
        nicsLabel.SetText(fmt.Sprintf("NICs: %d", len(ifs)))
    })

    // Rooms list (data-bound so it updates reliably on changes)
    roomsData := binding.NewStringList()
    syncRoomsBinding := func() {
        names := make([]string, 0, len(appConfig.Rooms))
        for _, r := range appConfig.Rooms {
            names = append(names, r.Name)
        }
        _ = roomsData.Set(names)
    }
    roomsList := widget.NewListWithData(
        roomsData,
        func() fyne.CanvasObject { return widget.NewLabel("room") },
        func(di binding.DataItem, o fyne.CanvasObject) {
            s := di.(binding.String)
            txt, _ := s.Get()
            o.(*widget.Label).SetText(txt)
        },
    )
    // initial populate
    syncRoomsBinding()

    addRoomBtn := widget.NewButton("Add Room", func() {
        entry := widget.NewEntry()
        entry.SetPlaceHolder("Room name (e.g., Living Room)")
        d := fyne.CurrentApp().NewWindow("Create Room")
        d.SetContent(container.NewVBox(
            widget.NewLabelWithStyle("Create Room", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}),
            entry,
            container.NewHBox(
                widget.NewButton("Cancel", func() { d.Close() }),
                widget.NewButton("Create", func() {
                    name := entry.Text
                    if name == "" { return }
                    // trim whitespace; avoid empty after trim
                    if t := strings.TrimSpace(name); t != "" { name = t } else { return }
                    // append and persist
                    appConfig.Rooms = append(appConfig.Rooms, cfg.RoomConfig{
                        Name:                  name,
                        AirplayName:           name,
                        BindInterfaceAirplay:  "",
                        BindInterfaceSpeakers: "",
                        TargetDeviceIDs:       []string{},
                    })
                    if err := cfg.Save(appConfig); err != nil {
                        fyne.CurrentApp().SendNotification(&fyne.Notification{Title: "Save Error", Content: err.Error()})
                    }
                    // refresh list and select the new room so it’s visible
                    syncRoomsBinding()
                    roomsList.Refresh()
                    roomsList.Select(len(appConfig.Rooms)-1)
                    d.Close()
                }),
            ),
        ))
        d.Resize(fyne.NewSize(420, 160))
        d.Show()
    })

    // Top toolbar
    checkBtn := widget.NewButton("Check System", func() {
        res := systemcheck.Run()
        msg := ""
        for _, d := range res.Details { msg += d+"\n" }
        if res.OK { msg = "OK\n"+msg }
        fyne.CurrentApp().SendNotification(&fyne.Notification{Title: "System Check", Content: msg})
    })

    top := container.NewHBox(
        detectBtn, engineLabel,
        refreshNicsBtn, nicsLabel,
        addRoomBtn, checkBtn,
    )
    top.Objects = append(top.Objects, widget.NewSeparator())

    // Left panel (rooms)
    left := container.NewBorder(nil, nil, nil, nil,
        container.NewVBox(widget.NewLabelWithStyle("Rooms", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}), roomsList))
    left.Resize(fyne.NewSize(240, 600))

    // Right panel placeholder
    rightTitle := widget.NewLabelWithStyle("Room Details", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})
    selectedIdx := -1
    airNic := widget.NewSelect([]string{}, func(string) {})
    spkNic := widget.NewSelect([]string{}, func(string) {})
    startBtn := widget.NewButton("Start", nil)
    stopBtn := widget.NewButton("Stop", nil)
    statusLbl := widget.NewLabel("Idle")
    speakerList := widget.NewList(func() int { if selectedIdx<0 { return 0 }; return len(appConfig.Rooms[selectedIdx].TargetDeviceIDs) }, func() fyne.CanvasObject { return widget.NewLabel("speaker") }, func(i widget.ListItemID, o fyne.CanvasObject) { if selectedIdx>=0 { o.(*widget.Label).SetText(appConfig.Rooms[selectedIdx].TargetDeviceIDs[i]) } })
    discoverBtn := widget.NewButton("Discover Speakers", nil)
    resolveBtn := widget.NewButton("Resolve Control URLs", nil)

    sup := rooms.NewSupervisor(engine.Detect())
    logsOut := widget.NewMultiLineEntry()
    logsOut.SetPlaceHolder("Container logs will appear here…")
    logsOut.Wrapping = fyne.TextWrapWord
    logsBtn := widget.NewButton("Tail Logs", func() {
        if selectedIdx < 0 { return }
        r := appConfig.Rooms[selectedIdx]
        txt, err := sup.Logs(roomID(r), 200)
        if err != nil { logsOut.SetText("Error: "+err.Error()); return }
        logsOut.SetText(txt)
    })

    refreshNicOptions := func() {
        ifs := netifaces.List()
        var names []string
        for _, i := range ifs { names = append(names, i.Name) }
        airNic.Options = names
        spkNic.Options = names
        airNic.Refresh(); spkNic.Refresh()
    }
    refreshNicOptions()

    roomsList.OnSelected = func(id widget.ListItemID) {
        selectedIdx = id
        if id >= 0 && id < len(appConfig.Rooms) {
            r := appConfig.Rooms[id]
            rightTitle.SetText("Room: "+r.Name)
            airNic.SetSelected(r.BindInterfaceAirplay)
            spkNic.SetSelected(r.BindInterfaceSpeakers)
        }
    }

    airNic.OnChanged = func(s string) {
        if selectedIdx >= 0 {
            appConfig.Rooms[selectedIdx].BindInterfaceAirplay = s
            _ = cfg.Save(appConfig)
        }
    }
    spkNic.OnChanged = func(s string) {
        if selectedIdx >= 0 {
            appConfig.Rooms[selectedIdx].BindInterfaceSpeakers = s
            _ = cfg.Save(appConfig)
        }
    }

    startBtn.OnTapped = func() {
        if selectedIdx < 0 { return }
        r := appConfig.Rooms[selectedIdx]
        // Ensure macvlan network for AirPlay containers on selected NIC
        netName, err := netsetup.EnsureMacvlanNetwork(engine.Detect(), r.BindInterfaceAirplay)
        if err != nil {
            // Fallbacks: if iface is wireless or creation failed, use host networking for reliability
            if netsetup.IsWireless(r.BindInterfaceAirplay) {
                netName = "host"
            } else {
                // As a last resort, still try host networking
                netName = "host"
            }
        }
        // Bind HTTP streamer to speaker NIC IP; stable per-room port 8090 + index
        ip, ok := netifaces.FirstIPv4(r.BindInterfaceSpeakers)
        if !ok { statusLbl.SetText("Select Speakers NIC"); return }
        port := 8090 + selectedIdx
        httpBind := fmt.Sprintf("%s:%d", ip, port)
        _ = netName // network selection used when starting the container
        // Avoid RTSP port conflicts when using host networking by assigning a unique port per room.
        raopPort := 0
        if netName == "host" { raopPort = 7000 + selectedIdx }
        if err := sup.StartRoom(roomID(r), r.AirplayName, netName, httpBind, raopPort); err != nil {
            statusLbl.SetText("Error: "+err.Error())
            return
        }
        // Kick UPnP targets (if any) with HTTP stream URL
        streamURL := fmt.Sprintf("http://%s:%d/stream", ip, port)
        for _, dev := range appConfig.Rooms[selectedIdx].TargetDeviceIDs {
            // Here 'dev' should be the AVTransport control URL; for now we treat it as such.
            _ = upnp.SetAVTransportURI(dev, streamURL, "")
            _ = upnp.Play(dev)
        }
        statusLbl.SetText("Running")
    }
    stopBtn.OnTapped = func() {
        if selectedIdx < 0 { return }
        r := appConfig.Rooms[selectedIdx]
        if err := sup.StopRoom(roomID(r)); err != nil { statusLbl.SetText("Error: "+err.Error()); return }
        statusLbl.SetText("Stopped")
    }

    discoverBtn.OnTapped = func() {
        if selectedIdx < 0 { return }
        ip, ok := netifaces.FirstIPv4(appConfig.Rooms[selectedIdx].BindInterfaceSpeakers)
        if !ok { statusLbl.SetText("Select Speakers NIC first"); return }
        // Discover generic UPnP renderers; users can copy their control URLs for now
        devs, err := ssdp.Discover(ip, "urn:schemas-upnp-org:device:MediaRenderer:1", 2*1e9)
        if err != nil { statusLbl.SetText("SSDP error: "+err.Error()); return }
        // Replace device IDs with their LOCATIONs for quick prototyping
        ids := make([]string, 0, len(devs))
        for _, d := range devs { ids = append(ids, d.Location) }
        appConfig.Rooms[selectedIdx].TargetDeviceIDs = ids
        _ = cfg.Save(appConfig)
        speakerList.Refresh()
    }
    resolveBtn.OnTapped = func() {
        if selectedIdx < 0 { return }
        var out []string
        for _, loc := range appConfig.Rooms[selectedIdx].TargetDeviceIDs {
            if ctrl, name, err := upnp.ResolveAVTransportControlURL(loc); err == nil {
                out = append(out, ctrl)
                log.Printf("%s -> %s", name, ctrl)
            } else {
                log.Printf("resolve failed for %s: %v", loc, err)
            }
        }
        if len(out) > 0 {
            appConfig.Rooms[selectedIdx].TargetDeviceIDs = out
            _ = cfg.Save(appConfig)
            speakerList.Refresh()
        }
    }

    right := container.NewVBox(
        rightTitle, widget.NewSeparator(),
        widget.NewLabel("AirPlay NIC"), airNic,
        widget.NewLabel("Speakers NIC"), spkNic,
        container.NewHBox(startBtn, stopBtn, statusLbl),
        widget.NewSeparator(),
        widget.NewLabelWithStyle("Speakers (UPnP - prototype)", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}),
        discoverBtn, resolveBtn,
        speakerList,
        widget.NewSeparator(),
        widget.NewLabelWithStyle("Logs", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}),
        logsBtn, logsOut,
    )

    // Split
    split := container.NewHSplit(left, right)
    split.Offset = 0.27

    content := container.NewBorder(top, nil, nil, nil, split)
    w.SetContent(content)

    // Initial detections
    detectBtn.OnTapped()
    refreshNicsBtn.OnTapped()

    // Basic quit handling
    w.SetCloseIntercept(func() {
        // Future: stop any supervised processes
        a.Quit()
        os.Exit(0)
    })

    // Minor background tweaks (keep defaults to maximize compatibility)
    _ = color.RGBA{R: 0x22, G: 0x22, B: 0x22, A: 0xff}

    w.ShowAndRun()
}

func roomID(r cfg.RoomConfig) string {
    // simple deterministic id based on name
    return sanitize(r.Name)
}

func sanitize(s string) string {
    out := make([]rune, 0, len(s))
    for _, r := range s {
        switch {
        case (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9'):
            out = append(out, r)
        case r == '-' || r == '_':
            out = append(out, r)
        case r == ' ':
            out = append(out, '-')
        }
    }
    if len(out) == 0 { return "room" }
    return string(out)
}


