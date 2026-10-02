int main(int argc,char **argv) {
  assert(argc==2);FILE *f=fopen(argv[1],"rb");assert(f);fseek(f,0,SEEK_END);long total=ftell(f);assert(total>0);fseek(f,0,SEEK_SET);uint8_t *data=malloc((size_t)total+10000);assert(data);assert(fread(data,1,(size_t)total,f)==(size_t)total);fclose(f);
  struct rtsp_message m;unsigned checks=0;size_t i;
  for(i=0;i<(size_t)total;i++){assert(rtsp_frame_parse(&m,data,i)==1);checks++;}
  assert(rtsp_frame_parse(&m,data,(size_t)total)==0&&m.bodylen==1282&&m.datalen==(size_t)total);checks++;
  memcpy(data+total,data,(size_t)total);assert(rtsp_frame_parse(&m,data,(size_t)total*2)==0&&m.datalen==(size_t)total);checks++;
  uint64_t value;assert(rtsp_uint(&value,(const uint8_t *)"65536",5,65536)==0&&value==65536);assert(rtsp_uint(&value,(const uint8_t *)"65537",5,65536)==-1);assert(rtsp_uint(&value,(const uint8_t *)"184467440737095516160",21,65536)==-1);checks+=3;
  const char *bad[]={"POST /bad RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\n\r\nx","POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\n\r\nx","POST /command RTSP/1.0\r\nContent-Length: 65537\r\nContent-Type: application/x-apple-binary-plist\r\n\r\n","POST /command RTSP/1.0\r\nContent-Length: 0\r\nContent-Type: application/x-apple-binary-plist\r\n\r\n","POST /command RTSP/1.0\r\nContent-Length: 1\r\nContent-Type: application/x-apple-binary-plist\r\nUnknown: x\ny\r\n\r\nx"};
  for(i=0;i<sizeof(bad)/sizeof(*bad);i++){assert(rtsp_frame_parse(&m,(const uint8_t *)bad[i],strlen(bad[i]))==-1);checks++;}
  memset(data,'x',8192);assert(rtsp_frame_parse(&m,data,8192)==-1);checks++;
  free(data);printf("event1 framing-only: %u exact-C retained-message/bounds checks PASS; plist/socket qualification not performed\n",checks);return 0;
}
