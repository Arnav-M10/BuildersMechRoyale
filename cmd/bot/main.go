// Minimal mechfight/1 stdin/stdout wrapper (written by our team) around the
// giddygiddyboy `mech` package (MIT License, see LICENSE-giddygiddyboy).
package main

import (
	"bufio"
	"encoding/json"
	"os"

	"giddybot/mech"
)

func main() {
	in := bufio.NewReaderSize(os.Stdin, 1<<20)
	out := bufio.NewWriter(os.Stdout)
	enc := json.NewEncoder(out)
	var brain *mech.Brain
	for {
		line, err := in.ReadBytes('\n')
		if len(line) > 0 {
			var head struct {
				Type string `json:"type"`
			}
			if json.Unmarshal(line, &head) == nil {
				switch head.Type {
				case "hello":
					var h mech.Hello
					if json.Unmarshal(line, &h) == nil {
						w, werr := mech.LoadWorld(h.Map.Dir)
						if werr != nil || w == nil {
							os.Stderr.WriteString("world load failed: " + h.Map.Dir + "\n")
							if werr != nil {
								os.Stderr.WriteString(werr.Error() + "\n")
							}
							w = mech.FlatWorld()
						} else {
							os.Stderr.WriteString("world loaded: " + h.Map.Dir + "\n")
						}
						brain = mech.NewBrain(&h, w)
					}
					enc.Encode(map[string]string{"type": "ready", "name": "larpers"})
					out.Flush()
				case "obs":
					var o mech.Obs
					ok := json.Unmarshal(line, &o) == nil
					// Skip to the newest fully-buffered observation, carrying events forward.
					for ok && in.Buffered() > 0 {
						peek, _ := in.Peek(in.Buffered())
						nl := -1
						for i, c := range peek {
							if c == '\n' {
								nl = i
								break
							}
						}
						if nl < 0 {
							break
						}
						next, _ := in.ReadBytes('\n')
						var nh struct {
							Type string `json:"type"`
						}
						if json.Unmarshal(next, &nh) != nil || nh.Type != "obs" {
							if nh.Type == "end" {
								return
							}
							continue
						}
						var n mech.Obs
						if json.Unmarshal(next, &n) == nil {
							n.Events = append(o.Events, n.Events...)
							o = n
						}
					}
					if brain != nil && ok {
						act := brain.Act(&o)
						enc.Encode(act)
					} else {
						enc.Encode(map[string]int{"tick": o.Tick})
					}
					out.Flush()
				case "end":
					return
				}
			}
		}
		if err != nil {
			return
		}
	}
}
